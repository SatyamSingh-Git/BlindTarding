"""Adversarial tests — hostile and malformed input.

The threat model is not an attacker; it is a bad afternoon. A truncated frame
during a reconnect, a NaN from a stale contract, a counter reset at midnight, a
CSV that changed shape overnight. None of these announce themselves, and every
one of them can produce a *plausible* wrong number that gets traded on.

The properties asserted here are the ones that matter under stress:

1. The parser never loops forever and never allocates on attacker-controlled
   lengths.
2. No non-finite or non-positive price ever reaches a candle.
3. Aggregation stays monotonic — a late or duplicated tick cannot rewrite a
   closed bar.
4. Resolution fails loudly rather than returning the wrong contract.
"""

from __future__ import annotations

import datetime as dt
import math
import random
import struct
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from orbot.broker.dhan_feed import parse_frame, parse_packet
from orbot.broker.instruments import InstrumentError, InstrumentRegistry
from orbot.constants import ExchangeSegment, FeedCode, InstrumentType, OptionType
from orbot.engine.candles import CandleSeries, resample
from orbot.models import Candle, Tick

IST = ZoneInfo("Asia/Kolkata")
HEADER = struct.Struct("<BHBI")


def hdr(code: int, msg_len: int, seg: int = 0, sid: int = 13) -> bytes:
    return HEADER.pack(code, msg_len, seg, sid)


def ticker(ltp: float, ltt: int = 1_700_000_000, seg: int = 0, sid: int = 13) -> bytes:
    return hdr(FeedCode.TICKER, 8, seg, sid) + struct.pack("<fi", ltp, ltt)


def at(hour: int, minute: int, second: int = 0, day: int = 14) -> dt.datetime:
    """Minutes past the hour may overflow — callers walk far past 59."""
    base = dt.datetime(2026, 8, day, 0, 0, 0, tzinfo=IST)
    return base + dt.timedelta(hours=hour, minutes=minute, seconds=second)


def tick(hour: int, minute: int, price: float, *, volume: int | None = None) -> Tick:
    return Tick("13", ExchangeSegment.IDX_I, price, at(hour, minute), volume=volume)


# =====================================================================
# Feed parser — termination, bounds, poisoned values
# =====================================================================
class TestParserTermination:
    """A parser that fails to advance hangs the whole feed thread."""

    def test_random_garbage_always_terminates(self):
        rng = random.Random(1234)
        for _ in range(2000):
            blob = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 200)))
            parse_frame(blob)  # must return, not hang

    def test_every_truncation_of_a_valid_frame_terminates(self):
        frame = ticker(100.0) + ticker(200.0, sid=25) + ticker(300.0, sid=51)
        for cut in range(len(frame) + 1):
            ticks = parse_frame(frame[:cut])
            assert len(ticks) == cut // 16  # only whole packets are trusted

    def test_zero_declared_length_still_advances(self):
        """msg_len=0 on an unknown code must not park the cursor."""
        blob = hdr(99, 0) + b"\x00" * 8 + ticker(500.0)
        parse_frame(blob)  # terminates

    def test_absurd_declared_length_stops_without_allocating(self):
        blob = hdr(99, 65535) + b"\x00" * 4
        assert parse_frame(blob) == []

    def test_all_zero_bytes_terminate(self):
        assert parse_frame(b"\x00" * 4096) is not None

    def test_header_only_no_payload(self):
        for code in (FeedCode.TICKER, FeedCode.QUOTE, FeedCode.FULL, FeedCode.OI):
            assert parse_frame(hdr(code, 8)) == []

    def test_offset_never_moves_backwards(self):
        rng = random.Random(99)
        for _ in range(500):
            blob = bytes(rng.getrandbits(8) for _ in range(rng.randint(8, 80)))
            offset, guard = 0, 0
            while offset < len(blob) and guard < 1000:
                _, nxt = parse_packet(blob, offset)
                assert nxt >= offset
                if nxt == offset:
                    break
                offset, guard = nxt, guard + 1
            assert guard < 1000


class TestPoisonedPrices:
    """A NaN LTP does not raise — it silently poisons ATR, HMA and every bar
    downstream, and every comparison against it returns False."""

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_prices_are_rejected(self, bad):
        assert parse_frame(ticker(bad)) == []

    @pytest.mark.parametrize("bad", [0.0, -1.0, -99_999.0])
    def test_non_positive_prices_are_rejected(self, bad):
        assert parse_frame(ticker(bad)) == []

    def test_a_poisoned_packet_does_not_drop_its_neighbours(self):
        ticks = parse_frame(ticker(float("nan")) + ticker(24_500.0, sid=25))
        assert [t.security_id for t in ticks] == ["25"]

    def test_valid_price_still_passes(self):
        assert len(parse_frame(ticker(0.05))) == 1  # a 5-paise option is legitimate


class TestHostileHeaders:
    def test_unknown_segment_code_is_dropped_not_guessed(self):
        assert parse_frame(ticker(100.0, seg=200)) == []

    def test_segment_code_6_is_unmapped(self):
        """Dhan skips 6 between MCX_COMM (5) and BSE_CURRENCY (7)."""
        assert parse_frame(ticker(100.0, seg=6)) == []

    def test_max_security_id_does_not_overflow(self):
        ticks = parse_frame(ticker(100.0, sid=4_294_967_295))
        assert ticks[0].security_id == "4294967295"

    def test_negative_epoch_falls_back_to_now(self):
        blob = hdr(FeedCode.TICKER, 8) + struct.pack("<fi", 100.0, -5)
        ticks = parse_frame(blob)
        assert len(ticks) == 1 and ticks[0].ts.tzinfo is not None


# =====================================================================
# Candle aggregation — ordering, overflow, resets
# =====================================================================
class TestAggregationUnderStress:
    def test_closed_bar_cannot_be_rewritten_by_a_late_tick(self):
        series = CandleSeries(5)
        series.on_tick(tick(9, 16, 100.0))
        series.on_tick(tick(9, 21, 110.0))       # closes the 09:15 bar
        snapshot = (series.bars[-1].high, series.bars[-1].low, series.bars[-1].close)
        for price in (1e9, 0.01, 50.0):
            series.on_tick(tick(9, 17, price))   # arrives late for a closed bar
        assert (series.bars[-1].high, series.bars[-1].low,
                series.bars[-1].close) == snapshot

    def test_shuffled_ticks_never_produce_inverted_bars(self):
        rng = random.Random(7)
        series = CandleSeries(5)
        ticks = [tick(9, 15 + (i % 20), 100.0 + rng.uniform(-5, 5)) for i in range(400)]
        rng.shuffle(ticks)
        for t in ticks:
            series.on_tick(t)
        for bar in list(series.bars) + ([series.current] if series.current else []):
            assert bar.high >= bar.low
            assert bar.low <= bar.open <= bar.high
            assert bar.low <= bar.close <= bar.high

    def test_bar_timestamps_stay_strictly_increasing(self):
        series = CandleSeries(5)
        for i in range(60):
            series.on_tick(tick(9, 15 + i, 100.0 + i))
        stamps = [b.ts for b in series.bars]
        assert stamps == sorted(stamps)
        assert len(set(stamps)) == len(stamps)

    def test_volume_never_goes_negative_under_any_sequence(self):
        rng = random.Random(11)
        series = CandleSeries(5)
        for i in range(500):
            vol = rng.choice([0, 1, 10**9, rng.randint(0, 10**6)])
            series.on_tick(tick(9, 15 + (i % 30), 100.0, volume=vol))
            if series.current:
                assert series.current.volume >= 0
        assert all(b.volume >= 0 for b in series.bars)

    def test_midnight_volume_reset_does_not_underflow(self):
        series = CandleSeries(5)
        series.on_tick(tick(9, 16, 100.0, volume=9_999_999))
        series.on_tick(tick(9, 17, 101.0, volume=0))
        assert series.current.volume == 0

    def test_day_rollover_starts_a_new_bar(self):
        series = CandleSeries(5)
        series.on_tick(Tick("13", ExchangeSegment.IDX_I, 100.0, at(15, 25, day=14)))
        closed = series.on_tick(Tick("13", ExchangeSegment.IDX_I, 105.0, at(9, 16, day=15)))
        assert closed is not None and closed.ts.day == 14
        assert series.current.ts.day == 15

    def test_flush_with_no_ticks_is_safe(self):
        assert CandleSeries(5).flush(now=at(15, 30)) is None

    def test_flush_far_in_the_future_closes_exactly_one_bar(self):
        series = CandleSeries(5)
        series.on_tick(tick(9, 16, 100.0))
        assert series.flush(now=at(23, 59)) is not None
        assert series.flush(now=at(23, 59)) is None

    def test_naive_timestamp_is_localised_not_rejected(self):
        series = CandleSeries(5)
        naive = Tick("13", ExchangeSegment.IDX_I, 100.0,
                     dt.datetime(2026, 8, 14, 9, 16))
        series.on_tick(naive)
        assert series.current is not None

    def test_max_bars_bound_holds(self):
        series = CandleSeries(1, max_bars=10)
        for i in range(200):
            series.on_tick(tick(9, 15 + i, 100.0 + i))
        assert len(series.bars) <= 10

    def test_identical_timestamps_collapse_into_one_bar(self):
        series = CandleSeries(5)
        for price in (100.0, 200.0, 50.0):
            series.on_tick(tick(9, 16, price))
        assert series.current.high == 200.0
        assert series.current.low == 50.0
        assert len(series.bars) == 0


class TestResampleEdges:
    def test_empty_input(self):
        assert resample([], 5) == []

    def test_ragged_tail_is_kept(self):
        bars = [Candle(ts=at(9, 15 + i), open=1, high=2, low=0, close=1) for i in range(7)]
        out = resample(bars, 5)
        assert len(out) == 2  # 5 + a partial 2, not silently truncated

    def test_factor_larger_than_input(self):
        bars = [Candle(ts=at(9, 15), open=1, high=2, low=0, close=1)]
        assert len(resample(bars, 100)) == 1

    def test_aggregated_high_low_bound_the_group(self):
        bars = [Candle(ts=at(9, 15 + i), open=10, high=10 + i, low=10 - i, close=10)
                for i in range(5)]
        out = resample(bars, 5)[0]
        assert out.high == 14 and out.low == 6


# =====================================================================
# Instrument registry — malformed and hostile CSV
# =====================================================================
HEADER_ROW = (
    "EXCH_ID,SEGMENT,SECURITY_ID,ISIN,INSTRUMENT,UNDERLYING_SECURITY_ID,UNDERLYING_SYMBOL,"
    "SYMBOL_NAME,DISPLAY_NAME,INSTRUMENT_TYPE,SERIES,LOT_SIZE,SM_EXPIRY_DATE,STRIKE_PRICE,"
    "OPTION_TYPE,TICK_SIZE,SM_FREEZE_QTY,"
)


def csv_file(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "s.csv"
    path.write_text("\n".join((HEADER_ROW, *rows)) + "\n", encoding="utf-8")
    return path


class TestMalformedScripMaster:
    def test_empty_file(self, tmp_path):
        path = tmp_path / "empty.csv"
        path.write_text("", encoding="utf-8")
        assert len(InstrumentRegistry.load(path)) == 0

    def test_header_only(self, tmp_path):
        assert len(InstrumentRegistry.load(csv_file(tmp_path))) == 0

    def test_row_with_missing_columns_is_skipped(self, tmp_path):
        reg = InstrumentRegistry.load(csv_file(tmp_path, "NSE,I,13"))
        assert len(reg) <= 1

    def test_unknown_exchange_is_skipped_not_defaulted(self, tmp_path):
        """Defaulting an unknown exchange would route orders to the wrong venue."""
        row = "XXX,Z,13,NA,INDEX,0,NIFTY,NIFTY,Nifty 50,INDEX,NA,1,NA,-0.01,XX,0.05,0,"
        assert len(InstrumentRegistry.load(csv_file(tmp_path, row))) == 0

    def test_unknown_instrument_type_is_skipped(self, tmp_path):
        row = "NSE,I,13,NA,WIDGET,0,NIFTY,NIFTY,Nifty,WIDGET,NA,1,NA,-0.01,XX,0.05,0,"
        assert len(InstrumentRegistry.load(csv_file(tmp_path, row))) == 0

    def test_blank_security_id_is_skipped(self, tmp_path):
        row = "NSE,I,,NA,INDEX,0,NIFTY,NIFTY,Nifty,INDEX,NA,1,NA,-0.01,XX,0.05,0,"
        assert len(InstrumentRegistry.load(csv_file(tmp_path, row))) == 0

    def test_garbage_numerics_fall_back_to_safe_defaults(self, tmp_path):
        row = ("NSE,I,13,NA,INDEX,0,NIFTY,NIFTY,Nifty,INDEX,NA,abc,NA,xyz,XX,junk,-,")
        inst = InstrumentRegistry.load(csv_file(tmp_path, row)).index("NIFTY")
        assert inst.lot_size == 1        # never 0 — a zero lot size means a zero-qty order
        assert inst.tick_size > 0
        assert inst.strike is None

    def test_zero_lot_size_is_floored_to_one(self, tmp_path):
        row = "NSE,D,1,NA,OPTIDX,0,NIFTY,NIFTY,N,OPTIDX,NA,0,2026-08-20,24000.0,CE,5.0,0,"
        reg = InstrumentRegistry.load(csv_file(tmp_path, row))
        assert reg.option("NIFTY", OptionType.CE, 24000.0, dt.date(2026, 8, 20)).lot_size == 1

    def test_negative_lot_size_is_floored(self, tmp_path):
        row = "NSE,D,1,NA,OPTIDX,0,NIFTY,NIFTY,N,OPTIDX,NA,-65,2026-08-20,24000.0,CE,5.0,0,"
        reg = InstrumentRegistry.load(csv_file(tmp_path, row))
        assert reg.option("NIFTY", OptionType.CE, 24000.0, dt.date(2026, 8, 20)).lot_size >= 1

    def test_unicode_and_quotes_survive(self, tmp_path):
        row = 'NSE,I,13,NA,INDEX,0,NIFTY,NIFTY,"Nifty 50 — ₹",INDEX,NA,1,NA,-0.01,XX,0.05,0,'
        assert InstrumentRegistry.load(csv_file(tmp_path, row)).index("NIFTY").security_id == "13"

    def test_duplicate_security_ids_do_not_crash(self, tmp_path):
        row = "NSE,I,13,NA,INDEX,0,NIFTY,NIFTY,Nifty,INDEX,NA,1,NA,-0.01,XX,0.05,0,"
        reg = InstrumentRegistry.load(csv_file(tmp_path, row, row, row))
        assert reg.index("NIFTY").security_id == "13"

    def test_option_row_missing_strike_is_not_indexed_as_tradeable(self, tmp_path):
        row = "NSE,D,1,NA,OPTIDX,0,NIFTY,NIFTY,N,OPTIDX,NA,65,2026-08-20,,CE,5.0,0,"
        reg = InstrumentRegistry.load(csv_file(tmp_path, row))
        assert reg.strikes("NIFTY", InstrumentType.OPTIDX, dt.date(2026, 8, 20),
                           OptionType.CE) == []


class TestResolutionFailsLoudly:
    """Every one of these must raise. Returning a near-miss contract is worse
    than returning nothing, because it trades."""

    @pytest.fixture
    def reg(self, tmp_path):
        rows = ["NSE,I,13,NA,INDEX,0,NIFTY,NIFTY,Nifty 50,INDEX,NA,1,0001-01-01,-0.01,XX,0.05,0,"]
        for strike in (24_000, 24_100, 24_200):
            rows.append(f"NSE,D,{strike},NA,OPTIDX,0,NIFTY,NIFTY,N{strike},OPTIDX,NA,65,"
                        f"2026-08-20,{strike}.0,CE,5.0,1800,")
        return InstrumentRegistry.load(csv_file(tmp_path, *rows))

    def test_missing_symbol_raises(self, reg):
        with pytest.raises(InstrumentError):
            reg.index("NOSUCHINDEX")

    def test_missing_security_id_raises(self, reg):
        with pytest.raises(InstrumentError):
            reg.get(ExchangeSegment.IDX_I, "999999")

    def test_exact_strike_that_does_not_exist_raises(self, reg):
        with pytest.raises(InstrumentError):
            reg.option("NIFTY", OptionType.CE, 24_050.0, dt.date(2026, 8, 20))

    def test_wrong_expiry_raises(self, reg):
        with pytest.raises(InstrumentError):
            reg.option("NIFTY", OptionType.CE, 24_000.0, dt.date(2026, 8, 27))

    def test_empty_put_chain_raises_rather_than_returning_a_call(self, reg):
        with pytest.raises(InstrumentError):
            reg.select_option("NIFTY", OptionType.PE, 24_100.0, dt.date(2026, 8, 20))

    def test_unknown_expiry_rule_raises(self, reg):
        with pytest.raises(InstrumentError):
            reg.select_expiry("NIFTY", InstrumentType.OPTIDX, "third_tuesday",
                              today=dt.date(2026, 8, 14))

    def test_null_sentinel_expiry_is_never_selectable(self, reg):
        """0001-01-01 must not become a tradeable expiry."""
        assert reg.index("NIFTY").expiry is None
        with pytest.raises(InstrumentError):
            reg.select_expiry("NIFTY", InstrumentType.FUTIDX, "nearest",
                              today=dt.date(2026, 8, 14))

    @pytest.mark.parametrize("spot", [0.0, -1.0, 1e12])
    def test_absurd_spot_clamps_to_the_chain_and_stays_in_it(self, reg, spot):
        inst = reg.select_option("NIFTY", OptionType.CE, spot, dt.date(2026, 8, 20))
        assert inst.strike in (24_000.0, 24_100.0, 24_200.0)

    def test_nan_spot_does_not_return_a_contract(self, reg):
        with pytest.raises((InstrumentError, ValueError)):
            reg.select_option("NIFTY", OptionType.CE, float("nan"), dt.date(2026, 8, 20))


# =====================================================================
# Order payload — the last thing before real money
# =====================================================================
class TestOrderPayloadSafety:
    def _intent(self, **kw):
        from orbot.constants import OrderType, ProductType, TransactionType
        from orbot.models import Instrument, OrderIntent
        inst = Instrument(security_id="45102", segment=ExchangeSegment.NSE_FNO,
                          instrument_type=InstrumentType.OPTIDX, symbol="NIFTY",
                          display_name="NIFTY CE", lot_size=65)
        defaults = dict(plan="p", instrument=inst,
                        transaction_type=TransactionType.BUY, quantity=65,
                        product_type=ProductType.INTRADAY, order_type=OrderType.MARKET)
        return OrderIntent(**{**defaults, **kw})

    def test_quantity_is_always_an_int(self):
        payload = self._intent(quantity=65.0).to_payload("100")
        assert isinstance(payload["quantity"], int)

    def test_price_is_rounded_to_paise(self):
        payload = self._intent(price=123.456789).to_payload("100")
        assert payload["price"] == 123.46

    def test_correlation_id_is_truncated_to_dhan_limit(self):
        payload = self._intent(correlation_id="x" * 100).to_payload("100")
        assert len(payload["correlationId"]) <= 30

    def test_trigger_price_only_sent_for_stop_orders(self):
        from orbot.constants import OrderType
        assert "triggerPrice" not in self._intent(trigger_price=99.0).to_payload("100")
        sl = self._intent(order_type=OrderType.STOP_LOSS, trigger_price=99.0)
        assert sl.to_payload("100")["triggerPrice"] == 99.0

    def test_segment_and_security_id_come_from_the_instrument(self):
        """These two fields decide what actually gets bought."""
        payload = self._intent().to_payload("100")
        assert payload["securityId"] == "45102"
        assert payload["exchangeSegment"] == "NSE_FNO"


class TestRateLimiter:
    def test_burst_beyond_the_cap_is_refused(self):
        from orbot.broker.dhan_client import DhanAPIError, _RateLimiter
        limiter = _RateLimiter(per_second=3, per_day=100)
        for _ in range(3):
            limiter.check()
        with pytest.raises(DhanAPIError):
            limiter.check()

    def test_daily_cap_is_enforced(self):
        from orbot.broker.dhan_client import DhanAPIError, _RateLimiter
        limiter = _RateLimiter(per_second=1000, per_day=5)
        for _ in range(5):
            limiter.check()
        with pytest.raises(DhanAPIError):
            limiter.check()
