"""Angel One adapter tests.

Two things dominate here, both silent failure modes:

**Paise scaling.** Every Angel price — feed ticks, scrip-master strikes, tick
sizes — arrives as an integer number of paise. Forgetting the divide-by-100 does
not raise; it produces a price 100x wrong that sizes an order, trips a stop, and
logs a plausible-looking number.

**Cross-broker id confusion.** Dhan's securityId and Angel's token are different
numbers for the same contract. A stale id after a broker switch resolves to a
real but unrelated instrument, which is a wrong trade that looks entirely normal.
"""

from __future__ import annotations

import datetime as dt
import json
import struct
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from orbot.broker.angel.auth import AngelAuth, AngelAuthError, AngelSession
from orbot.broker.angel.client import (
    NATIVE_INTERVALS,
    _parse_candles,
    _parse_order,
    _parse_position,
)
from orbot.broker.angel.feed import (
    MODE_LTP,
    MODE_QUOTE,
    MODE_SNAP_QUOTE,
    PACKET_SIZES,
    PriceScaleCheck,
    parse_frame,
    parse_packet,
)
from orbot.broker.angel.instruments import (
    exchange_name,
    exchange_type_code,
    load_registry,
    parse_expiry,
    parse_row,
)
from orbot.broker.factory import check_plan_ids
from orbot.constants import ExchangeSegment, InstrumentType, OptionType, OrderStatus

IST = ZoneInfo("Asia/Kolkata")
TOTP_SECRET = "JBSWY3DPEHPK3PXP"


def packet(mode: int, token: str, ltp_paise: int, *, exchange: int = 2,
           ts_ms: int = 1_800_000_000_000, **kw) -> bytes:
    head = struct.pack("<BB25sqq", mode, exchange,
                       token.encode().ljust(25, b"\x00"), 1, ts_ms)
    body = struct.pack("<q", ltp_paise)
    if mode >= MODE_QUOTE:
        body += struct.pack(
            "<qqqddqqqq", kw.get("ltq", 75), kw.get("atp", 12_400),
            kw.get("volume", 900_000), 5.0, 7.0,
            kw.get("open", 12_000), kw.get("high", 13_000),
            kw.get("low", 11_800), kw.get("close", 12_200))
    if mode == MODE_SNAP_QUOTE:
        body += struct.pack("<q", 0)                       # last traded time
        body += struct.pack("<q", kw.get("oi", 1_234_500))  # open interest
        body += b"\x00" * (PACKET_SIZES[MODE_SNAP_QUOTE] - 43 - len(body))
    return head + body


def nfo(token: str, exchange_type: int) -> ExchangeSegment:
    return ExchangeSegment.NSE_FNO


# =====================================================================
class TestPacketSizes:
    def test_sizes_match_the_sdk(self):
        assert PACKET_SIZES == {1: 51, 2: 123, 3: 379}

    @pytest.mark.parametrize("mode,size", [(1, 51), (2, 123), (3, 379)])
    def test_built_packets_are_exact(self, mode, size):
        assert len(packet(mode, "45102", 12_575)) == size


class TestPaiseScaling:
    """The number that must not be wrong."""

    def test_ltp_is_divided_by_one_hundred(self):
        tick = parse_frame(packet(MODE_LTP, "45102", 12_575),
                           segment_lookup=nfo)[0]
        assert tick.ltp == pytest.approx(125.75)

    def test_every_ohlc_field_is_scaled(self):
        tick = parse_frame(packet(MODE_QUOTE, "45102", 12_575),
                           segment_lookup=nfo)[0]
        assert tick.day_open == pytest.approx(120.0)
        assert tick.day_high == pytest.approx(130.0)
        assert tick.day_low == pytest.approx(118.0)
        assert tick.day_close == pytest.approx(122.0)
        assert tick.atp == pytest.approx(124.0)

    def test_quantities_are_not_scaled(self):
        """Volume and quantity are counts, not prices — scaling them would be
        just as wrong in the other direction."""
        tick = parse_frame(packet(MODE_QUOTE, "45102", 12_575, volume=900_000),
                           segment_lookup=nfo)[0]
        assert tick.volume == 900_000
        assert tick.ltq == 75

    def test_an_index_level_scales_correctly(self):
        tick = parse_frame(packet(MODE_LTP, "99926000", 2_428_700, exchange=1),
                           segment_lookup=nfo)[0]
        assert tick.ltp == pytest.approx(24_287.0)

    def test_a_five_paise_option_survives(self):
        tick = parse_frame(packet(MODE_LTP, "45102", 5), segment_lookup=nfo)[0]
        assert tick.ltp == pytest.approx(0.05)


class TestFeedParsing:
    def test_the_token_is_read_as_a_null_terminated_string(self):
        tick = parse_frame(packet(MODE_LTP, "99926000", 100), segment_lookup=nfo)[0]
        assert tick.security_id == "99926000"

    def test_timestamps_are_milliseconds_converted_to_ist(self):
        tick = parse_frame(packet(MODE_LTP, "1", 100, ts_ms=1_800_000_000_000),
                           segment_lookup=nfo)[0]
        assert tick.ts.utcoffset().total_seconds() == 5.5 * 3600

    def test_a_zero_timestamp_falls_back_to_now(self):
        tick = parse_frame(packet(MODE_LTP, "1", 100, ts_ms=0),
                           segment_lookup=nfo)[0]
        assert tick.ts.tzinfo is not None

    def test_snap_quote_carries_open_interest(self):
        tick = parse_frame(packet(MODE_SNAP_QUOTE, "45102", 12_575, oi=1_234_500),
                           segment_lookup=nfo)[0]
        assert tick.oi == 1_234_500

    def test_multiple_packets_in_one_frame(self):
        frame = (packet(MODE_LTP, "13", 100) + packet(MODE_QUOTE, "45102", 200)
                 + packet(MODE_LTP, "25", 300))
        assert len(parse_frame(frame, segment_lookup=nfo)) == 3

    def test_a_truncated_frame_stops_cleanly(self):
        frame = packet(MODE_LTP, "13", 100) + packet(MODE_QUOTE, "45102", 200)[:40]
        assert len(parse_frame(frame, segment_lookup=nfo)) == 1

    @pytest.mark.parametrize("paise", [0, -1, -50_000])
    def test_non_positive_prices_are_dropped(self, paise):
        assert parse_frame(packet(MODE_LTP, "45102", paise), segment_lookup=nfo) == []

    def test_an_unknown_mode_stops_rather_than_desyncing(self):
        assert parse_frame(packet(MODE_LTP, "1", 100).replace(
            b"\x01", b"\x63", 1)) == []

    def test_empty_and_garbage_frames_terminate(self):
        import random
        assert parse_frame(b"") == []
        rng = random.Random(5)
        for _ in range(300):
            parse_frame(bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 120))))

    def test_offset_never_goes_backwards(self):
        import random
        rng = random.Random(11)
        for _ in range(200):
            blob = bytes(rng.getrandbits(8) for _ in range(rng.randint(43, 200)))
            offset, guard = 0, 0
            while offset < len(blob) and guard < 500:
                _, nxt = parse_packet(blob, offset)
                assert nxt >= offset
                if nxt == offset:
                    break
                offset, guard = nxt, guard + 1


class TestSegmentRecovery:
    """Exchange type 1 covers NSE cash *and* indices, so a packet alone cannot
    tell them apart. The segment comes from what we subscribed."""

    def test_the_subscription_decides_the_segment(self):
        from orbot.broker.angel.feed import AngelFeed, AngelFeedInstrument
        auth = AngelAuth("C1", "key")
        feed = AngelFeed(auth)
        feed.add([AngelFeedInstrument(ExchangeSegment.IDX_I, "99926000")])
        assert feed._segment_for("99926000", 1) is ExchangeSegment.IDX_I

    def test_an_unsubscribed_token_falls_back_to_the_code(self):
        from orbot.broker.angel.feed import AngelFeed
        feed = AngelFeed(AngelAuth("C1", "key"))
        assert feed._segment_for("999", 2) is ExchangeSegment.NSE_FNO


class TestPriceScaleCheck:
    """The runtime guard against the paise divisor being wrong."""

    def test_agreement_passes(self):
        check = PriceScaleCheck()
        assert check.verify(125.75, 125.80)

    def test_a_hundred_times_too_large_is_caught_and_named(self):
        check = PriceScaleCheck()
        assert not check.verify(12_575.0, 125.75)
        assert "not applied" in check.detail

    def test_a_hundred_times_too_small_is_caught_and_named(self):
        check = PriceScaleCheck()
        assert not check.verify(1.2575, 125.75)
        assert "twice" in check.detail

    def test_no_reference_fails_closed(self):
        check = PriceScaleCheck()
        assert not check.verify(125.75, None)
        assert "no reference" in check.detail


# =====================================================================
ANGEL_ROWS = [
    {"token": "99926000", "symbol": "Nifty 50", "name": "NIFTY", "expiry": "",
     "strike": "-1.000000", "lotsize": "1", "instrumenttype": "AMXIDX",
     "exch_seg": "NSE", "tick_size": "0.000000"},
    {"token": "2885", "symbol": "RELIANCE-EQ", "name": "RELIANCE", "expiry": "",
     "strike": "-1.000000", "lotsize": "1", "instrumenttype": "",
     "exch_seg": "NSE", "tick_size": "5.000000"},
    {"token": "58072", "symbol": "NIFTY27AUG26FUT", "name": "NIFTY",
     "expiry": "27AUG2026", "strike": "-1.000000", "lotsize": "65",
     "instrumenttype": "FUTIDX", "exch_seg": "NFO", "tick_size": "5.000000"},
]
for _strike in range(24_000, 25_001, 100):
    for _opt in ("CE", "PE"):
        ANGEL_ROWS.append({
            "token": f"{45000 + _strike // 100}{1 if _opt == 'CE' else 2}",
            "symbol": f"NIFTY20AUG26{_strike}{_opt}", "name": "NIFTY",
            "expiry": "20AUG2026", "strike": f"{_strike * 100}.000000",
            "lotsize": "65", "instrumenttype": "OPTIDX", "exch_seg": "NFO",
            "tick_size": "5.000000"})


@pytest.fixture
def registry(tmp_path: Path):
    path = tmp_path / "angel.json"
    path.write_text(json.dumps(ANGEL_ROWS), encoding="utf-8")
    return load_registry(path)


class TestInstrumentParsing:
    def test_strike_is_converted_from_paise(self):
        """A NIFTY 24300 CE carries strike '2430000' in the file."""
        row = next(r for r in ANGEL_ROWS if r["symbol"] == "NIFTY20AUG2624300CE")
        inst = parse_row(row)
        assert inst.strike == pytest.approx(24_300.0)

    def test_tick_size_is_converted_from_paise(self):
        inst = parse_row(ANGEL_ROWS[2])
        assert inst.tick_size == pytest.approx(0.05)

    def test_an_index_is_classified_as_such(self):
        inst = parse_row(ANGEL_ROWS[0])
        assert inst.segment is ExchangeSegment.IDX_I
        assert inst.instrument_type is InstrumentType.INDEX
        assert inst.strike is None and inst.expiry is None

    def test_an_equity_row_is_recognised_by_its_eq_suffix(self):
        inst = parse_row(ANGEL_ROWS[1])
        assert inst.segment is ExchangeSegment.NSE_EQ
        assert inst.instrument_type is InstrumentType.EQUITY
        assert inst.series == "EQ"

    def test_fno_rows_map_to_nse_fno(self):
        assert parse_row(ANGEL_ROWS[2]).segment is ExchangeSegment.NSE_FNO

    def test_option_type_comes_from_the_symbol_suffix(self):
        ce = parse_row(next(r for r in ANGEL_ROWS if r["symbol"].endswith("24300CE")))
        pe = parse_row(next(r for r in ANGEL_ROWS if r["symbol"].endswith("24300PE")))
        assert ce.option_type is OptionType.CE
        assert pe.option_type is OptionType.PE

    def test_lot_size_is_read(self):
        assert parse_row(ANGEL_ROWS[2]).lot_size == 65

    @pytest.mark.parametrize("raw,expected", [
        ("27AUG2026", dt.date(2026, 8, 27)),
        ("20AUG2026", dt.date(2026, 8, 20)),
        ("", None), ("NA", None), ("garbage", None),
    ])
    def test_expiry_formats(self, raw, expected):
        assert parse_expiry(raw) == expected

    def test_unknown_rows_are_skipped_not_guessed(self):
        assert parse_row({"token": "1", "exch_seg": "XXX",
                          "instrumenttype": "WIDGET", "symbol": "X"}) is None

    def test_a_row_with_no_token_is_skipped(self):
        assert parse_row({"token": "", "exch_seg": "NSE", "symbol": "X-EQ"}) is None


class TestRegistryReuse:
    """Angel rows feed the same registry the Dhan adapter uses, so strike and
    expiry selection behave identically across brokers."""

    def test_index_lookup(self, registry):
        assert registry.index("NIFTY").security_id == "99926000"

    def test_equity_lookup(self, registry):
        assert registry.equity("RELIANCE").security_id == "2885"

    def test_atm_selection_uses_rupee_strikes(self, registry):
        expiry = dt.date(2026, 8, 20)
        inst = registry.select_option("NIFTY", OptionType.CE, 24_287.0, expiry)
        assert inst.strike == pytest.approx(24_300.0)
        assert inst.lot_size == 65

    def test_itm_offset_behaves_as_on_dhan(self, registry):
        expiry = dt.date(2026, 8, 20)
        inst = registry.select_option("NIFTY", OptionType.CE, 24_500.0, expiry,
                                      offset=2)
        assert inst.strike == pytest.approx(24_300.0)

    def test_future_lookup(self, registry):
        assert registry.future("NIFTY", dt.date(2026, 8, 27)).security_id == "58072"

    def test_expiry_selection(self, registry):
        chosen = registry.select_expiry("NIFTY", InstrumentType.OPTIDX, "nearest",
                                        today=dt.date(2026, 8, 14))
        assert chosen == dt.date(2026, 8, 20)


class TestExchangeMapping:
    @pytest.mark.parametrize("segment,name", [
        (ExchangeSegment.NSE_EQ, "NSE"), (ExchangeSegment.NSE_FNO, "NFO"),
        (ExchangeSegment.BSE_EQ, "BSE"), (ExchangeSegment.BSE_FNO, "BFO"),
        (ExchangeSegment.MCX_COMM, "MCX"), (ExchangeSegment.IDX_I, "NSE"),
    ])
    def test_rest_exchange_names(self, segment, name):
        assert exchange_name(segment) == name

    @pytest.mark.parametrize("segment,code", [
        (ExchangeSegment.NSE_EQ, 1), (ExchangeSegment.IDX_I, 1),
        (ExchangeSegment.NSE_FNO, 2), (ExchangeSegment.BSE_EQ, 3),
        (ExchangeSegment.BSE_FNO, 4), (ExchangeSegment.MCX_COMM, 5),
    ])
    def test_feed_exchange_codes(self, segment, code):
        assert exchange_type_code(segment) == code


class TestResponseParsing:
    def test_candles_carry_an_explicit_offset(self):
        """Angel returns ISO strings with +05:30 — none of Dhan's epoch
        ambiguity to resolve."""
        candles = _parse_candles([
            ["2026-08-14T09:15:00+05:30", 24_200, 24_300, 24_150, 24_280, 12_000]])
        assert len(candles) == 1
        assert candles[0].ts.hour == 9 and candles[0].ts.minute == 15
        assert candles[0].ts.utcoffset().total_seconds() == 5.5 * 3600
        assert candles[0].close == 24_280

    def test_candle_prices_are_not_paise_scaled(self):
        """Historical prices come in rupees, unlike the feed. Scaling them
        would make every bar 100x too small."""
        candles = _parse_candles([["2026-08-14T09:15:00+05:30", 125.5, 130.0,
                                   120.0, 128.0, 900]])
        assert candles[0].open == pytest.approx(125.5)

    def test_malformed_candle_rows_are_skipped(self):
        assert _parse_candles([["bad"], None, {}, ["2026-08-14T09:15:00+05:30",
                                                  1, 2, 3, 4, 5]]) != []

    @pytest.mark.parametrize("raw,expected", [
        ("complete", OrderStatus.TRADED),
        ("rejected", OrderStatus.REJECTED),
        ("cancelled", OrderStatus.CANCELLED),
        ("open", OrderStatus.PENDING),
        ("trigger pending", OrderStatus.TRIGGERED),
        ("something new", OrderStatus.TRANSIT),
    ])
    def test_order_status_mapping(self, raw, expected):
        assert _parse_order({"orderid": "1", "status": raw}).status is expected

    def test_order_fields(self):
        order = _parse_order({"orderid": "250814000123", "status": "complete",
                              "symboltoken": "45102", "exchange": "NFO",
                              "transactiontype": "BUY", "quantity": "65",
                              "filledshares": "65", "averageprice": "125.75"})
        assert order.filled_qty == 65
        assert order.avg_price == pytest.approx(125.75)
        assert order.segment is ExchangeSegment.NSE_FNO

    def test_position_fields(self):
        position = _parse_position({"symboltoken": "45102", "exchange": "NFO",
                                    "netqty": "65", "avgnetprice": "120.5",
                                    "producttype": "INTRADAY"})
        assert position.net_qty == 65
        assert position.segment is ExchangeSegment.NSE_FNO


class TestAngelAuth:
    def test_full_credentials_enable_self_login(self):
        auth = AngelAuth("C1", "key", mpin="1234", totp_secret=TOTP_SECRET)
        assert auth.can_self_renew

    @pytest.mark.parametrize("kwargs", [{"mpin": "1234"}, {"totp_secret": TOTP_SECRET}, {}])
    def test_partial_credentials_do_not(self, kwargs):
        assert not AngelAuth("C1", "key", **kwargs).can_self_renew

    def test_no_session_raises_with_guidance(self):
        with pytest.raises(AngelAuthError, match="ANGEL_MPIN"):
            AngelAuth("C1", "key").jwt()

    def test_login_stores_all_three_tokens(self, monkeypatch, tmp_path):
        monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(
            200, json={"status": True, "data": {
                "jwtToken": "Bearer jwt-123", "refreshToken": "refresh-456",
                "feedToken": "feed-789"}}))
        auth = AngelAuth("C1", "key", mpin="1234", totp_secret=TOTP_SECRET,
                         cache_path=tmp_path / "s.json")
        session = auth.login()
        assert session.jwt_token == "jwt-123"     # Bearer prefix stripped
        assert session.refresh_token == "refresh-456"
        assert session.feed_token == "feed-789"

    def test_the_feed_token_is_distinct_from_the_jwt(self, monkeypatch):
        """The WebSocket will not authenticate with the REST jwt."""
        monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(
            200, json={"status": True, "data": {
                "jwtToken": "jwt", "refreshToken": "r", "feedToken": "feed"}}))
        auth = AngelAuth("C1", "key", mpin="1234", totp_secret=TOTP_SECRET)
        auth.login()
        assert auth.jwt() == "jwt" and auth.feed_token() == "feed"

    def test_a_rejected_login_raises_with_guidance(self, monkeypatch):
        monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(
            200, json={"status": False, "message": "Invalid totp"}))
        auth = AngelAuth("C1", "key", mpin="1234", totp_secret=TOTP_SECRET)
        with pytest.raises(AngelAuthError):
            auth.login()

    def test_a_refresh_that_omits_the_feed_token_keeps_the_old_one(self, monkeypatch):
        """Losing the feed token on refresh would silently break the market feed
        at the next reconnect."""
        auth = AngelAuth("C1", "key", mpin="1234", totp_secret=TOTP_SECRET)
        auth._session = AngelSession("old-jwt", "refresh-1", "feed-original",
                                     client_code="C1")
        monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(
            200, json={"status": True, "data": {"jwtToken": "new-jwt"}}))
        renewed = auth.refresh()
        assert renewed.jwt_token == "new-jwt"
        assert renewed.feed_token == "feed-original"

    def test_required_headers_are_present(self):
        headers = AngelAuth("C1", "my-key").base_headers()
        for key in ("X-PrivateKey", "X-ClientLocalIP", "X-ClientPublicIP",
                    "X-MACAddress", "X-UserType", "X-SourceID"):
            assert headers.get(key)
        assert headers["X-PrivateKey"] == "my-key"

    def test_a_session_cache_for_another_client_is_ignored(self, tmp_path):
        cache = tmp_path / "s.json"
        cache.write_text(json.dumps({"client_code": "OTHER", "jwt_token": "x"}))
        assert AngelAuth("C1", "key", cache_path=cache).session is None

    @pytest.mark.parametrize("content", ["", "{bad", "[]"])
    def test_a_corrupt_session_cache_is_ignored(self, tmp_path, content):
        cache = tmp_path / "s.json"
        cache.write_text(content)
        assert AngelAuth("C1", "key", cache_path=cache).session is None


class TestBrokerMismatch:
    """Dhan and Angel use different ids for the same contract, and a stale one
    resolves to a real but unrelated instrument."""

    def test_dhan_ids_under_angel_are_flagged(self):
        problems = check_plan_ids("angel", ["13", "25"])
        assert problems and "99926000" in problems[0]

    def test_angel_tokens_under_dhan_are_flagged(self):
        assert check_plan_ids("dhan", ["99926000"])

    def test_matching_ids_pass(self):
        assert check_plan_ids("dhan", ["13", "2885"]) == []
        assert check_plan_ids("angel", ["99926000", "2885"]) == []


class TestNativeIntervals:
    def test_angel_offers_more_native_intervals_than_dhan(self):
        from orbot.constants import INTRADAY_INTERVALS
        assert set(NATIVE_INTERVALS) > {5, 15, 60}
        assert 10 in NATIVE_INTERVALS and 30 in NATIVE_INTERVALS
        assert 10 not in INTRADAY_INTERVALS and 30 not in INTRADAY_INTERVALS


class TestHostileTimestamps:
    """A garbage timestamp must not take the feed reader down. datetime raises
    OSError on absurd epoch values, and one malformed packet killing the reader
    would stop the whole feed mid-session."""

    @pytest.mark.parametrize("ts_ms", [
        -1, 1, 2**62, -(2**62), 999_999_999_999_999, 1,
    ])
    def test_absurd_timestamps_fall_back_to_now(self, ts_ms):
        ticks = parse_frame(packet(MODE_LTP, "45102", 12_575, ts_ms=ts_ms),
                            segment_lookup=nfo)
        assert len(ticks) == 1
        assert ticks[0].ts.tzinfo is not None

    def test_a_plausible_timestamp_is_preserved(self):
        # 2026-08-14 09:15:00 IST
        stamp = dt.datetime(2026, 8, 14, 9, 15, tzinfo=IST)
        ticks = parse_frame(packet(MODE_LTP, "45102", 12_575,
                                   ts_ms=int(stamp.timestamp() * 1000)),
                            segment_lookup=nfo)
        assert ticks[0].ts.hour == 9 and ticks[0].ts.minute == 15
