"""Candle aggregation tests.

The three failure modes worth guarding: bars anchored to midnight instead of the
session open, volume double-counted from cumulative readings, and bars that
never close because nothing traded.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from orbot.constants import ExchangeSegment
from orbot.engine.candles import CandleSeries, resample
from orbot.models import Candle, Tick

IST = ZoneInfo("Asia/Kolkata")


def at(hour: int, minute: int, second: int = 0, day: int = 14) -> dt.datetime:
    return dt.datetime(2026, 8, day, hour, minute, second, tzinfo=IST)


def tick(hour: int, minute: int, price: float, *, volume: int | None = None,
         second: int = 0) -> Tick:
    return Tick(security_id="13", segment=ExchangeSegment.IDX_I, ltp=price,
                ts=at(hour, minute, second), volume=volume)


class TestBarAlignment:
    def test_5m_bars_anchor_to_session_open(self):
        series = CandleSeries(5)
        assert series.bar_start(at(9, 17)) == at(9, 15)
        assert series.bar_start(at(9, 20)) == at(9, 20)
        assert series.bar_start(at(9, 24, 59)) == at(9, 20)

    def test_60m_bars_run_from_0915_not_0900(self):
        """The case that catches a midnight-anchored implementation: TradingView
        shows 09:15-10:15 for NSE, not 09:00-10:00."""
        series = CandleSeries(60)
        assert series.bar_start(at(9, 30)) == at(9, 15)
        assert series.bar_start(at(10, 14)) == at(9, 15)
        assert series.bar_start(at(10, 16)) == at(10, 15)

    def test_25m_bars_anchor_to_session_open(self):
        series = CandleSeries(25)
        assert series.bar_start(at(9, 15)) == at(9, 15)
        assert series.bar_start(at(9, 39)) == at(9, 15)
        assert series.bar_start(at(9, 40)) == at(9, 40)

    def test_15m_matches_the_midnight_grid_by_coincidence(self):
        # 09:15 is 555 minutes, divisible by 15 — both anchorings agree here,
        # which is exactly why this bug hides until someone uses a 25m or 1h chart.
        series = CandleSeries(15)
        assert series.bar_start(at(9, 29)) == at(9, 15)
        assert series.bar_start(at(9, 30)) == at(9, 30)

    def test_preopen_tick_falls_on_previous_session_grid(self):
        series = CandleSeries(5)
        assert series.bar_start(at(9, 0)) < at(9, 15)


class TestAggregation:
    def test_ohlc_within_one_bar(self):
        series = CandleSeries(5)
        for minute, price in ((15, 100.0), (16, 105.0), (17, 95.0), (18, 102.0)):
            assert series.on_tick(tick(9, minute, price)) is None
        current = series.current
        assert (current.open, current.high, current.low, current.close) == (
            100.0, 105.0, 95.0, 102.0)

    def test_bar_closes_when_next_bar_starts(self):
        series = CandleSeries(5)
        series.on_tick(tick(9, 16, 100.0))
        series.on_tick(tick(9, 19, 110.0))
        closed = series.on_tick(tick(9, 21, 111.0))

        assert closed is not None
        assert closed.ts == at(9, 15)
        assert closed.close == 110.0
        assert closed.closed is True
        assert series.current.ts == at(9, 20)
        assert series.current.open == 111.0

    def test_late_tick_for_closed_bar_is_ignored(self):
        series = CandleSeries(5)
        series.on_tick(tick(9, 16, 100.0))
        series.on_tick(tick(9, 21, 110.0))   # closes 09:15 bar
        before = len(series.bars)
        assert series.on_tick(tick(9, 17, 999.0)) is None
        assert len(series.bars) == before
        assert series.bars[-1].high == 100.0  # the stale price did not leak in


class TestVolume:
    def test_volume_is_a_delta_of_cumulative_readings(self):
        """The Quote packet carries running day volume. Summing it would inflate
        every bar."""
        series = CandleSeries(5)
        series.on_tick(tick(9, 16, 100.0, volume=1000))  # baseline only
        series.on_tick(tick(9, 17, 101.0, volume=1500))  # +500
        series.on_tick(tick(9, 18, 102.0, volume=1800))  # +300
        assert series.current.volume == 800

    def test_counter_reset_does_not_produce_negative_volume(self):
        series = CandleSeries(5)
        series.on_tick(tick(9, 16, 100.0, volume=5000))
        series.on_tick(tick(9, 17, 101.0, volume=10))  # reconnect / new session
        assert series.current.volume >= 0

    def test_missing_volume_is_tolerated(self):
        series = CandleSeries(5)
        series.on_tick(tick(9, 16, 100.0))
        assert series.current.volume == 0


class TestFlush:
    def test_flush_closes_an_elapsed_bar_with_no_further_ticks(self):
        """An illiquid strike may not print for a whole bar. Without this the
        signal would wait for the next trade."""
        series = CandleSeries(5)
        series.on_tick(tick(9, 16, 100.0))
        assert series.flush(now=at(9, 18)) is None      # still inside the window
        closed = series.flush(now=at(9, 20, 1))
        assert closed is not None and closed.ts == at(9, 15)

    def test_flush_is_idempotent(self):
        series = CandleSeries(5)
        series.on_tick(tick(9, 16, 100.0))
        assert series.flush(now=at(9, 21)) is not None
        assert series.flush(now=at(9, 21)) is None


class TestSeed:
    def test_seeded_bars_are_marked_closed(self):
        series = CandleSeries(5)
        seeded = [Candle(ts=at(9, 15), open=1, high=2, low=0.5, close=1.5),
                  Candle(ts=at(9, 20), open=1.5, high=2.5, low=1.0, close=2.0)]
        assert series.seed(seeded) == 2
        assert all(b.closed for b in series.bars)
        assert series.closes() == [1.5, 2.0]

    def test_seed_ignores_bars_already_present(self):
        series = CandleSeries(5)
        series.seed([Candle(ts=at(9, 15), open=1, high=2, low=0.5, close=1.5)])
        added = series.seed([Candle(ts=at(9, 15), open=9, high=9, low=9, close=9),
                             Candle(ts=at(9, 20), open=2, high=3, low=1, close=2.5)])
        assert added == 1
        assert series.closes() == [1.5, 2.5]


class TestResample:
    def test_five_one_minute_bars_become_one_five_minute_bar(self):
        bars = [Candle(ts=at(9, 15 + i), open=10 + i, high=20 + i, low=5 + i, close=15 + i,
                       volume=100) for i in range(5)]
        out = resample(bars, 5)
        assert len(out) == 1
        assert out[0].ts == at(9, 15)
        assert out[0].open == 10
        assert out[0].close == 19
        assert out[0].high == 24
        assert out[0].low == 5
        assert out[0].volume == 500

    def test_factor_one_is_a_passthrough(self):
        bars = [Candle(ts=at(9, 15), open=1, high=1, low=1, close=1)]
        assert resample(bars, 1) == bars
