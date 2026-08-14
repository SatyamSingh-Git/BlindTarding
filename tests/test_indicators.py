"""Pine-semantics tests for the indicator port.

Each test targets a specific way the obvious Python translation differs from
what Pine actually computes. None of these differences raise an error — they
shift a value slightly, which moves a crossover by a bar, which moves an entry.
"""

from __future__ import annotations

import datetime as dt
import math
from zoneinfo import ZoneInfo

import pytest

from orbot.engine.indicators import pine
from orbot.engine.indicators.hma import hma, hma_with_slope
from orbot.engine.indicators.orb import in_session_flags, opening_range, parse_session
from orbot.engine.indicators.utbot import ut_bot
from orbot.engine.strategy import CHART_DEFAULTS, StrategyParams, SvmkrUtHmaOrb
from orbot.models import Candle

IST = ZoneInfo("Asia/Kolkata")


def bars(prices, *, start=dt.datetime(2026, 8, 14, 9, 15, tzinfo=IST), minutes=5,
         spread=1.0):
    return [
        Candle(ts=start + dt.timedelta(minutes=minutes * i),
               open=p, high=p + spread, low=p - spread, close=p, volume=100)
        for i, p in enumerate(prices)
    ]


class TestPineRound:
    """Pine rounds half away from zero; Python rounds half to even."""

    def test_disagrees_with_python_on_exact_halves(self):
        assert pine.pine_round(14.5) == 15
        assert round(14.5) == 14        # the trap being avoided
        assert pine.pine_round(15.5) == 16

    @pytest.mark.parametrize("value,expected", [
        (0.4, 0), (0.5, 1), (1.5, 2), (2.5, 3), (5.5677, 6),
        (-0.5, -1), (-1.5, -2),
    ])
    def test_values(self, value, expected):
        assert pine.pine_round(value) == expected

    def test_hma_window_lengths_at_the_configured_length(self):
        """n=31 -> inner windows of 16 and 31, outer window of 6."""
        assert pine.pine_round(31 / 2) == 16
        assert pine.pine_round(math.sqrt(31)) == 6


class TestMovingAverages:
    def test_wma_weights_the_newest_bar_heaviest(self):
        # (1*1 + 2*2 + 3*3) / 6 = 14/6
        assert pine.wma([1.0, 2.0, 3.0], 3)[-1] == pytest.approx(14 / 6)

    def test_wma_is_na_until_the_window_fills(self):
        out = pine.wma([1.0, 2.0, 3.0], 3)
        assert out[0] is None and out[1] is None and out[2] is not None

    def test_rma_at_length_one_is_the_identity(self):
        """The configured ATR period is 1, so this is the live path."""
        src = [5.0, 3.0, 9.0, 1.0]
        assert pine.rma(src, 1) == src

    def test_rma_seeds_with_an_sma_then_smooths(self):
        src = [2.0, 4.0, 6.0, 8.0]
        out = pine.rma(src, 3)
        assert out[0] is None and out[1] is None
        assert out[2] == pytest.approx(4.0)                     # SMA seed
        assert out[3] == pytest.approx((1 / 3) * 8 + (2 / 3) * 4)

    def test_rma_is_not_sma(self):
        """Using a rolling mean for ATR is the classic port bug."""
        src = [1.0, 2.0, 3.0, 100.0]
        assert pine.rma(src, 3)[-1] != pytest.approx(pine.sma(src, 3)[-1])

    def test_ema_at_length_one_is_the_identity(self):
        """`ema(src, 1)` in the source is a no-op — alpha is 2/(1+1) = 1."""
        src = [10.0, 20.0, 15.0]
        assert pine.ema(src, 1) == src


class TestTrueRangeAndATR:
    def test_first_bar_falls_back_to_high_minus_low(self):
        tr = pine.true_range([10.0], [8.0], [9.0])
        assert tr[0] == pytest.approx(2.0)

    def test_gap_up_uses_the_previous_close(self):
        # Bar 2 gaps away: high 20, low 19, prev close 10.
        # TR = max(20-19, |20-10|, |19-10|) = 10, i.e. the gap dominates the
        # bar's own range. Using high-low alone would report 1.
        tr = pine.true_range([10.0, 20.0], [8.0, 19.0], [10.0, 19.5])
        assert tr[1] == pytest.approx(10.0)

    def test_gap_down_uses_the_previous_close(self):
        tr = pine.true_range([10.0, 5.0], [8.0, 4.0], [10.0, 4.5])
        assert tr[1] == pytest.approx(6.0)     # |4 - 10|

    def test_atr_at_period_one_equals_true_range(self):
        highs, lows, closes = [10.0, 12.0, 11.0], [8.0, 9.0, 10.0], [9.0, 11.0, 10.5]
        assert pine.atr(highs, lows, closes, 1) == pine.true_range(highs, lows, closes)


class TestCrossover:
    def test_previous_bar_uses_less_than_or_equal(self):
        """Pine's crossover is `a > b and a[1] <= b[1]` — a touch then a break
        counts as a cross. Using `<` would miss it."""
        a = [5.0, 5.0, 6.0]
        b = [5.0, 5.0, 5.0]
        assert pine.crossover(a, b) == [False, False, True]

    def test_no_cross_when_already_above(self):
        assert pine.crossover([6.0, 7.0], [5.0, 5.0]) == [False, False]

    def test_first_bar_is_never_a_cross(self):
        assert pine.crossover([9.0], [1.0]) == [False]

    def test_na_comparisons_are_false(self):
        assert pine.crossover([None, 5.0], [None, 1.0]) == [False, False]


class TestHMA:
    def test_matches_the_manual_formula(self):
        prices = [float(i) for i in range(1, 60)]
        n = 31
        half = pine.wma(prices, pine.pine_round(n / 2))
        full = pine.wma(prices, n)
        diff = [None if (a is None or b is None) else 2 * a - b
                for a, b in zip(half, full)]
        expected = pine.wma(diff, pine.pine_round(math.sqrt(n)))
        assert hma(prices, n) == expected

    def test_lag_on_a_ramp_matches_the_analytic_value_exactly(self):
        """A WMA of length L lags a unit ramp by exactly (L-1)/3.

        In the HMA the `2*WMA(n/2) - WMA(n)` stage cancels lag to zero
        (2*(15/3) - 30/3 == 0 at n=31), leaving only the outer WMA of length
        `round(sqrt(31)) == 6`, which lags by 5/3. So the HMA of a unit ramp
        sits exactly 1.667 below the current price — not on top of it.

        Asserted to 1e-9 rather than approximately: any drift here means a
        window length or a weighting is wrong.
        """
        prices = [100.0 + i for i in range(120)]
        out = hma(prices, 31)
        sqn = pine.pine_round(math.sqrt(31))
        assert out[-1] == pytest.approx(prices[-1] - (sqn - 1) / 3, abs=1e-9)

    def test_wma_lag_on_a_ramp_is_the_textbook_value(self):
        """The building block the test above depends on."""
        prices = [float(i) for i in range(60)]
        for length in (3, 6, 16, 31):
            out = pine.wma(prices, length)
            assert out[-1] == pytest.approx(prices[-1] - (length - 1) / 3, abs=1e-9)

    def test_n2_is_n1_delayed_by_one_bar(self):
        """The source builds a second HMA from `close[1]`; that is just a shift."""
        prices = [100.0 + math.sin(i / 5) * 10 for i in range(120)]
        n1, n2, _ = hma_with_slope(prices, 31)
        assert n2[1:] == n1[:-1]

    def test_rising_flag_follows_slope(self):
        up = hma_with_slope([100.0 + i for i in range(120)], 31)[2]
        down = hma_with_slope([300.0 - i for i in range(120)], 31)[2]
        assert up[-1] is True
        assert down[-1] is False


class TestORBSession:
    def test_parses_the_configured_session(self):
        assert parse_session("1010-1015") == (dt.time(10, 10), dt.time(10, 15))

    def test_ignores_the_day_mask(self):
        assert parse_session("1010-1015:1234567") == (dt.time(10, 10), dt.time(10, 15))

    @pytest.mark.parametrize("bad", ["", "1010", "abcd-1015", "9999-1015", "1010-1060"])
    def test_malformed_specs_raise(self, bad):
        with pytest.raises(ValueError):
            parse_session(bad)

    def test_end_of_session_is_exclusive(self):
        """A 10:15 bar is outside a 1010-1015 window — on a 5m chart the window
        is exactly the single 10:10 bar."""
        stamps = [dt.datetime(2026, 8, 14, 10, m, tzinfo=IST) for m in (5, 10, 15, 20)]
        assert in_session_flags(stamps, dt.time(10, 10), dt.time(10, 15)) == [
            False, True, False, False]

    def test_one_minute_chart_has_five_in_session_bars(self):
        stamps = [dt.datetime(2026, 8, 14, 10, m, tzinfo=IST) for m in range(8, 18)]
        assert sum(in_session_flags(stamps, dt.time(10, 10), dt.time(10, 15))) == 5

    def test_levels_are_none_before_the_session(self):
        stamps = [dt.datetime(2026, 8, 14, 9, 15 + 5 * i, tzinfo=IST) for i in range(3)]
        res = opening_range(stamps, [10.0, 11.0, 12.0], [9.0, 9.5, 10.0], "1010-1015")
        assert res.high == [None, None, None]

    def test_levels_set_on_the_session_bar_then_carry_forward(self):
        stamps = [dt.datetime(2026, 8, 14, 10, m, tzinfo=IST) for m in (5, 10, 15, 20)]
        res = opening_range(stamps, [10.0, 20.0, 30.0, 40.0],
                            [5.0, 15.0, 1.0, 2.0], "1010-1015")
        assert res.high == [None, 20.0, 20.0, 20.0]   # frozen after the session
        assert res.low == [None, 15.0, 15.0, 15.0]

    def test_range_expands_within_a_multi_bar_session(self):
        stamps = [dt.datetime(2026, 8, 14, 10, m, tzinfo=IST) for m in range(10, 16)]
        highs = [20.0, 25.0, 22.0, 30.0, 21.0, 99.0]
        lows = [15.0, 14.0, 16.0, 10.0, 17.0, 1.0]
        res = opening_range(stamps, highs, lows, "1010-1015")
        assert res.high[4] == 30.0     # widest inside the window
        assert res.low[4] == 10.0
        assert res.high[5] == 30.0     # 10:15 bar is outside; no expansion


class TestUTBot:
    def test_stop_ratchets_up_and_never_falls_in_an_uptrend(self):
        prices = [100.0 + i for i in range(50)]
        res = ut_bot([p + 1 for p in prices], [p - 1 for p in prices], prices,
                     key_value=3.0, atr_period=1)
        stops = [s for s in res.stop if s is not None]
        assert all(stops[i] >= stops[i - 1] for i in range(1, len(stops)))

    def test_stop_stays_below_price_while_long(self):
        prices = [100.0 + i for i in range(50)]
        res = ut_bot([p + 1 for p in prices], [p - 1 for p in prices], prices,
                     key_value=3.0, atr_period=1)
        assert all(res.stop[i] < prices[i] for i in range(10, 50))

    def test_buy_reduces_to_a_crossover_of_price_over_the_stop(self):
        """`ema(src,1)` is the identity and `crossover` already implies `>`, so
        `buy` collapses to `crossover(close, stop)`. Asserted so a future
        'simplification' cannot silently change behaviour."""
        prices = [100, 101, 99, 97, 95, 98, 103, 107, 104, 101, 99, 96, 100, 105]
        prices = [float(p) for p in prices]
        res = ut_bot([p + 1 for p in prices], [p - 1 for p in prices], prices,
                     key_value=3.0, atr_period=1)
        assert res.buy == pine.crossover(prices, res.stop)

    def test_a_bar_is_never_both_buy_and_sell(self):
        prices = [100.0 + 10 * math.sin(i / 3) for i in range(200)]
        res = ut_bot([p + 1 for p in prices], [p - 1 for p in prices], prices,
                     key_value=3.0, atr_period=1)
        assert not any(res.buy[i] and res.sell[i] for i in range(len(prices)))

    def test_a_larger_key_value_produces_fewer_signals(self):
        """Key value is described in the source as the sensitivity control."""
        prices = [100.0 + 5 * math.sin(i / 4) for i in range(300)]
        highs, lows = [p + 1 for p in prices], [p - 1 for p in prices]
        loose = ut_bot(highs, lows, prices, key_value=1.0, atr_period=1)
        tight = ut_bot(highs, lows, prices, key_value=6.0, atr_period=1)
        assert sum(tight.buy) <= sum(loose.buy)

    def test_signals_alternate_direction(self):
        """A crossover cannot follow a crossover without a crossunder between."""
        prices = [100.0 + 8 * math.sin(i / 6) for i in range(400)]
        res = ut_bot([p + 1 for p in prices], [p - 1 for p in prices], prices,
                     key_value=3.0, atr_period=1)
        seq = [("B" if res.buy[i] else "S")
               for i in range(len(prices)) if res.buy[i] or res.sell[i]]
        assert all(seq[i] != seq[i - 1] for i in range(1, len(seq)))


class TestStrategyComposition:
    """The headline finding: HMA and ORB do not gate the signal."""

    def _prices(self):
        return [100.0 + 8 * math.sin(i / 7) + (i % 5) * 0.3 for i in range(300)]

    def test_defaults_match_the_chart_legend_not_the_pine_defaults(self):
        """Legend reads "3 1 31 5 1010-1015"; the Pine source defaults key to 2."""
        assert CHART_DEFAULTS["ut_key"] == 3.0
        assert StrategyParams().ut_key == 3.0
        assert StrategyParams().ut_atr_period == 1
        assert StrategyParams().hma_length == 31
        assert StrategyParams().orb_session == "1010-1015"

    def test_signals_are_identical_to_raw_ut_bot(self):
        candles = bars(self._prices())
        result = SvmkrUtHmaOrb().evaluate(candles)
        assert result.buy == result.raw_buy == result.ut.buy
        assert result.sell == result.raw_sell == result.ut.sell

    def test_changing_hma_length_does_not_move_a_single_signal(self):
        candles = bars(self._prices())
        a = SvmkrUtHmaOrb(StrategyParams(hma_length=31)).evaluate(candles)
        b = SvmkrUtHmaOrb(StrategyParams(hma_length=9)).evaluate(candles)
        assert a.buy == b.buy and a.sell == b.sell
        assert a.hma != b.hma      # the HMA itself did change

    def test_changing_the_orb_session_does_not_move_a_single_signal(self):
        candles = bars(self._prices())
        a = SvmkrUtHmaOrb(StrategyParams(orb_session="1010-1015")).evaluate(candles)
        b = SvmkrUtHmaOrb(StrategyParams(orb_session="0915-0930")).evaluate(candles)
        assert a.buy == b.buy and a.sell == b.sell

    def test_ut_key_does_move_signals(self):
        """Control for the two tests above — the harness can detect a change."""
        candles = bars(self._prices())
        a = SvmkrUtHmaOrb(StrategyParams(ut_key=1.0)).evaluate(candles)
        b = SvmkrUtHmaOrb(StrategyParams(ut_key=8.0)).evaluate(candles)
        assert a.buy != b.buy

    def test_optional_filters_only_remove_signals(self):
        candles = bars(self._prices())
        base = SvmkrUtHmaOrb().evaluate(candles)
        filtered = SvmkrUtHmaOrb(
            StrategyParams(require_hma_agreement=True)).evaluate(candles)
        assert sum(filtered.buy) <= sum(base.buy)
        assert all(base.buy[i] for i in range(len(candles)) if filtered.buy[i])

    def test_orb_filter_blocks_everything_before_the_session_exists(self):
        """A None ORB level must not read as a passed breakout check."""
        candles = bars(self._prices(),
                       start=dt.datetime(2026, 8, 14, 9, 15, tzinfo=IST))
        res = SvmkrUtHmaOrb(StrategyParams(require_orb_breakout=True)).evaluate(candles)
        pre_session = [i for i, ts in enumerate(res.timestamps)
                       if ts.time() < dt.time(10, 10)]
        assert not any(res.buy[i] for i in pre_session)

    def test_latest_signal_refuses_to_fire_on_a_cold_start(self):
        strat = SvmkrUtHmaOrb()
        assert strat.latest_signal(bars([100.0, 101.0, 102.0])) is None

    def test_signals_carry_the_bar_timestamp_for_deduplication(self):
        candles = bars(self._prices())
        signals = SvmkrUtHmaOrb(plan="test").signals(candles)
        assert signals
        assert all(s.bar_ts in set(c.ts for c in candles) for s in signals)
        assert len({s.dedupe_key for s in signals}) == len(signals)

    def test_empty_input_does_not_crash(self):
        assert SvmkrUtHmaOrb().evaluate([]).buy == []
        assert SvmkrUtHmaOrb().signals([]) == []
