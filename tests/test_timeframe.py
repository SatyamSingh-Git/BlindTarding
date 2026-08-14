"""Timeframe selection and resampling.

The user picks a timeframe at runtime, anywhere from 5 minutes to 2 days. Dhan
serves only 1/5/15/25/60-minute and daily natively, so everything else is built
by resampling — and resampling is where timeframes go quietly wrong.

The central hazard: an NSE session is 375 minutes. That divides evenly by 5, 15
and 25 but **not** by 30 (12.5 bars) or 60 (6.25 bars). A resampler that groups
every N bars therefore drifts a fraction of a bar per day and eventually emits a
bar that opens before one day's close and shuts after the next day's open,
straddling an overnight gap. No chart anywhere shows that bar, and the ATR it
feeds would be nonsense. Bucketing is by session-anchored wall clock instead.
"""

from __future__ import annotations

import datetime as dt
import random
from zoneinfo import ZoneInfo

import pytest

from orbot.engine.candles import resample_to
from orbot.engine.timeframe import (
    MAX_DAYS,
    MIN_INTRADAY_MINUTES,
    Timeframe,
    TimeframeError,
    describe_support,
    supported,
)
from orbot.models import Candle

IST = ZoneInfo("Asia/Kolkata")
SESSION_BARS_5M = 75          # 09:15-15:30 inclusive of the 15:25 bar


def at(hour: int, minute: int, second: int = 0, day: int = 14) -> dt.datetime:
    """Minutes may overflow past 59 — callers walk whole sessions."""
    base = dt.datetime(2026, 8, day, 0, 0, tzinfo=IST)
    return base + dt.timedelta(hours=hour, minutes=minute, seconds=second)


def session_bars(day: int, *, step: int = 5, count: int = SESSION_BARS_5M,
                 price: float = 100.0) -> list[Candle]:
    return [Candle(ts=at(9, 15 + step * i, day=day), open=price, high=price + 1,
                   low=price - 1, close=price, volume=1) for i in range(count)]


class TestParsing:
    @pytest.mark.parametrize("spec,label", [
        ("5m", "5m"), ("5", "5m"), (5, "5m"), ("15min", "15m"), ("15 minutes", "15m"),
        ("60m", "1h"), ("1h", "1h"), ("2hr", "2h"), ("4hours", "4h"),
        ("1D", "1D"), ("1d", "1D"), ("2d", "2D"), ("1day", "1D"),
    ])
    def test_accepted_forms(self, spec, label):
        assert Timeframe.parse(spec).label == label

    def test_sixty_minutes_normalises_to_one_hour(self):
        assert Timeframe.parse("60m") == Timeframe.parse("1h")

    @pytest.mark.parametrize("spec", ["1m", "2m", "3m", "4m", "0m", "-5m"])
    def test_below_the_floor_is_refused(self, spec):
        with pytest.raises(TimeframeError):
            Timeframe.parse(spec)

    @pytest.mark.parametrize("spec", ["3D", "7d", "30d"])
    def test_above_the_ceiling_is_refused(self, spec):
        with pytest.raises(TimeframeError):
            Timeframe.parse(spec)

    @pytest.mark.parametrize("spec", ["", "abc", "5x", "m5", "5.5m", "1W", "1M", "  "])
    def test_garbage_is_refused(self, spec):
        with pytest.raises(TimeframeError):
            Timeframe.parse(spec)

    def test_bounds_are_what_was_asked_for(self):
        assert MIN_INTRADAY_MINUTES == 5
        assert MAX_DAYS == 2

    def test_parse_is_idempotent(self):
        tf = Timeframe.parse("15m")
        assert Timeframe.parse(tf) is tf


class TestSourcing:
    """Which timeframes come straight from Dhan and which we build ourselves.
    The distinction drives whether the validation gate can trust the bars."""

    @pytest.mark.parametrize("spec", ["5m", "15m", "25m", "1h", "1D"])
    def test_native_intervals(self, spec):
        assert Timeframe.parse(spec).is_native

    @pytest.mark.parametrize("spec", ["10m", "20m", "30m", "45m", "2h", "3h", "4h", "2D"])
    def test_resampled_intervals(self, spec):
        assert not Timeframe.parse(spec).is_native

    def test_base_interval_divides_the_target_exactly(self):
        for tf in supported():
            if not tf.is_intraday:
                continue
            assert tf.minutes % tf.base_interval == 0
            assert tf.base_interval * tf.resample_factor == tf.minutes

    def test_base_interval_is_always_a_dhan_native_one(self):
        from orbot.constants import INTRADAY_INTERVALS
        for tf in supported():
            if tf.is_intraday:
                assert tf.base_interval in INTRADAY_INTERVALS

    def test_largest_usable_base_is_chosen(self):
        """30m builds from 15m x 2, not 5m x 6 — fewer bars to fetch and fewer
        aggregation steps to get wrong."""
        assert Timeframe.parse("30m").base_interval == 15
        assert Timeframe.parse("45m").base_interval == 15
        assert Timeframe.parse("2h").base_interval == 60

    def test_an_unbuildable_interval_is_refused_not_approximated(self):
        with pytest.raises(TimeframeError):
            Timeframe.parse("7m").base_interval

    def test_daily_uses_no_intraday_interval(self):
        assert Timeframe.parse("1D").base_interval is None
        assert Timeframe.parse("2D").resample_factor == 2

    def test_every_supported_timeframe_is_describable(self):
        rows = describe_support()
        assert len(rows) == len(supported())
        assert all(len(row) == 3 and all(row) for row in rows)


class TestIntradayResampling:
    def test_five_one_minute_bars_become_one_five_minute_bar(self):
        bars = [Candle(ts=at(9, 15 + i), open=10 + i, high=20 + i, low=5 + i,
                       close=15 + i, volume=100) for i in range(5)]
        out = resample_to(bars, Timeframe.parse("5m"))
        assert len(out) == 1
        assert (out[0].ts, out[0].open, out[0].close) == (at(9, 15), 10, 19)
        assert out[0].high == 24 and out[0].low == 5
        assert out[0].volume == 500

    def test_same_timeframe_is_a_passthrough(self):
        bars = session_bars(14)
        assert len(resample_to(bars, Timeframe.parse("5m"))) == len(bars)

    def test_hourly_bars_anchor_to_the_session_open(self):
        """TradingView runs NSE hourly bars 09:15-10:15, not 09:00-10:00."""
        out = resample_to(session_bars(14), Timeframe.parse("1h"))
        assert [c.ts.time() for c in out][:3] == [
            dt.time(9, 15), dt.time(10, 15), dt.time(11, 15)]

    def test_no_bar_ever_spans_two_days(self):
        """The bug that count-based grouping would produce."""
        bars = session_bars(14) + session_bars(17)
        for spec in ("30m", "45m", "1h", "2h", "4h"):
            out = resample_to(bars, Timeframe.parse(spec))
            assert len(out) > 1, spec
            for candle in out:
                members_days = {b.ts.date() for b in bars
                                if candle.ts <= b.ts < candle.ts
                                + dt.timedelta(minutes=Timeframe.parse(spec).minutes)}
                assert len(members_days) <= 1, f"{spec} bar {candle.ts} spans {members_days}"

    def test_every_session_starts_a_fresh_grid(self):
        bars = session_bars(14) + session_bars(17) + session_bars(18)
        out = resample_to(bars, Timeframe.parse("30m"))
        by_day: dict[dt.date, list[Candle]] = {}
        for candle in out:
            by_day.setdefault(candle.ts.date(), []).append(candle)
        assert len(by_day) == 3
        for day_bars in by_day.values():
            assert day_bars[0].ts.time() == dt.time(9, 15)
            # 375 / 30 = 12.5, so 13 buckets with a short final one.
            assert len(day_bars) == 13

    def test_ragged_tail_is_kept_not_truncated(self):
        bars = [Candle(ts=at(9, 15 + i), open=1, high=2, low=0, close=1) for i in range(7)]
        assert len(resample_to(bars, Timeframe.parse("5m"))) == 2

    def test_ohlc_bounds_every_member_bar(self):
        rng = random.Random(3)
        bars = [Candle(ts=at(9, 15 + i), open=100, high=100 + rng.random() * 10,
                       low=100 - rng.random() * 10, close=100) for i in range(60)]
        tf = Timeframe.parse("15m")
        for candle in resample_to(bars, tf):
            members = [b for b in bars
                       if candle.ts <= b.ts < candle.ts + dt.timedelta(minutes=15)]
            assert candle.high == max(b.high for b in members)
            assert candle.low == min(b.low for b in members)
            assert candle.open == members[0].open
            assert candle.close == members[-1].close
            assert candle.volume == sum(b.volume for b in members)

    def test_output_is_strictly_increasing(self):
        bars = session_bars(14) + session_bars(17) + session_bars(18)
        out = resample_to(bars, Timeframe.parse("45m"))
        stamps = [c.ts for c in out]
        assert stamps == sorted(stamps)
        assert len(set(stamps)) == len(stamps)

    def test_unsorted_input_is_ordered_first(self):
        bars = [Candle(ts=at(9, 15 + i), open=i, high=i, low=i, close=i) for i in range(5)]
        out = resample_to(list(reversed(bars)), Timeframe.parse("5m"))
        assert out[0].open == 0 and out[0].close == 4

    def test_duplicate_timestamps_collapse(self):
        bars = [Candle(ts=at(9, 15), open=1, high=5, low=0, close=2),
                Candle(ts=at(9, 15), open=9, high=9, low=9, close=9)]
        out = resample_to(bars, Timeframe.parse("5m"))
        assert len(out) == 1 and out[0].high == 9

    def test_empty_input(self):
        assert resample_to([], Timeframe.parse("5m")) == []

    def test_single_bar(self):
        bars = [Candle(ts=at(9, 15), open=1, high=2, low=0, close=1)]
        assert len(resample_to(bars, Timeframe.parse("4h"))) == 1


class TestDailyResampling:
    def test_two_day_bars_group_consecutive_sessions(self):
        bars = [Candle(ts=at(9, 15, day=d), open=10, high=20, low=5, close=15, volume=7)
                for d in (14, 17, 18, 19)]
        out = resample_to(bars, Timeframe.parse("2D"))
        assert len(out) == 2
        assert [c.ts.day for c in out] == [14, 18]
        assert out[0].volume == 14

    def test_a_holiday_shifts_the_grid_rather_than_tearing_it(self):
        """Grouped by trading-day index, not calendar date, so a missing session
        does not leave a gap or misalign every later bar."""
        bars = [Candle(ts=at(9, 15, day=d), open=10, high=20, low=5, close=15)
                for d in (14, 17, 20, 21)]        # 18-19 absent
        out = resample_to(bars, Timeframe.parse("2D"))
        assert [c.ts.day for c in out] == [14, 20]

    def test_odd_session_count_keeps_the_final_partial_bar(self):
        bars = [Candle(ts=at(9, 15, day=d), open=1, high=2, low=0, close=1)
                for d in (14, 17, 18)]
        assert len(resample_to(bars, Timeframe.parse("2D"))) == 2

    def test_one_day_is_a_passthrough(self):
        bars = [Candle(ts=at(9, 15, day=d), open=1, high=2, low=0, close=1)
                for d in (14, 17, 18)]
        assert len(resample_to(bars, Timeframe.parse("1D"))) == 3


class TestFrequencyExpectations:
    def test_bars_per_session_matches_the_nse_session_length(self):
        assert Timeframe.parse("5m").approx_bars_per_session == pytest.approx(75)
        assert Timeframe.parse("15m").approx_bars_per_session == pytest.approx(25)
        assert Timeframe.parse("1h").approx_bars_per_session == pytest.approx(6.25)

    def test_slower_timeframes_have_fewer_bars(self):
        rates = [tf.approx_bars_per_session for tf in supported()]
        assert rates == sorted(rates, reverse=True)
