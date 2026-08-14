"""Binary feed parser tests.

A wrong byte offset here does not crash — it produces plausible-looking garbage
prices, which is the worst possible failure for a trading system. These tests
build packets to the published spec and assert exact round-trips.
"""

from __future__ import annotations

import struct

import pytest

from orbot.broker.dhan_feed import parse_frame, parse_packet
from orbot.constants import ExchangeSegment, FeedCode

HEADER = struct.Struct("<BHBI")


def header(code: int, payload_len: int, segment: ExchangeSegment, security_id: int) -> bytes:
    return HEADER.pack(code, payload_len, segment.code, security_id)


def ticker_packet(ltp: float, ltt: int, *, security_id: int = 13,
                  segment: ExchangeSegment = ExchangeSegment.IDX_I) -> bytes:
    return header(FeedCode.TICKER, 8, segment, security_id) + struct.pack("<fi", ltp, ltt)


def quote_packet(
    ltp: float = 24_500.5, ltq: int = 50, ltt: int = 1_700_000_000, atp: float = 24_499.0,
    volume: int = 123_456, sell_qty: int = 10, buy_qty: int = 20,
    d_open: float = 24_400.0, d_close: float = 24_450.0,
    d_high: float = 24_600.0, d_low: float = 24_380.0,
    *, security_id: int = 1333, segment: ExchangeSegment = ExchangeSegment.NSE_EQ,
) -> bytes:
    return header(FeedCode.QUOTE, 42, segment, security_id) + struct.pack(
        "<fhifiiiffff", ltp, ltq, ltt, atp, volume, sell_qty, buy_qty,
        d_open, d_close, d_high, d_low,
    )


def full_packet(*, security_id: int = 35084,
                segment: ExchangeSegment = ExchangeSegment.NSE_FNO) -> bytes:
    body = struct.pack(
        "<fhifiiiiiiffff",
        125.75, 75, 1_700_000_000, 124.5, 900_000, 5, 7, 1_234_500, 1_300_000, 1_100_000,
        120.0, 122.0, 130.0, 118.0,
    )
    depth = b"".join(
        struct.pack("<iihhff", 100 + i, 200 + i, 3, 4, 125.0 + i, 125.5 + i)
        for i in range(5)
    )
    return header(FeedCode.FULL, 154, segment, security_id) + body + depth


class TestPacketSizes:
    """Total sizes are fixed by the spec: ticker 16, quote 50, full 162, OI 12."""

    def test_ticker_is_16_bytes(self):
        assert len(ticker_packet(24_500.0, 1_700_000_000)) == 16

    def test_quote_is_50_bytes(self):
        assert len(quote_packet()) == 50

    def test_full_is_162_bytes(self):
        assert len(full_packet()) == 162


class TestTicker:
    def test_round_trip(self):
        tick, offset = parse_packet(ticker_packet(24_500.25, 1_700_000_000))
        assert offset == 16
        assert tick is not None
        assert tick.security_id == "13"
        assert tick.segment is ExchangeSegment.IDX_I
        assert tick.ltp == pytest.approx(24_500.25, abs=0.01)

    def test_segment_code_decodes(self):
        tick, _ = parse_packet(
            ticker_packet(100.0, 1_700_000_000, segment=ExchangeSegment.NSE_FNO)
        )
        assert tick.segment is ExchangeSegment.NSE_FNO


class TestQuote:
    def test_all_fields_land_in_the_right_place(self):
        tick, offset = parse_packet(quote_packet())
        assert offset == 50
        assert tick.ltp == pytest.approx(24_500.5, abs=0.01)
        assert tick.ltq == 50
        assert tick.volume == 123_456
        assert tick.atp == pytest.approx(24_499.0, abs=0.01)
        assert tick.day_open == pytest.approx(24_400.0, abs=0.01)
        assert tick.day_close == pytest.approx(24_450.0, abs=0.01)
        assert tick.day_high == pytest.approx(24_600.0, abs=0.01)
        assert tick.day_low == pytest.approx(24_380.0, abs=0.01)

    def test_ohlc_ordering_is_not_transposed(self):
        """Guards against swapping the open/close/high/low block, which would
        still parse cleanly but be wrong."""
        tick, _ = parse_packet(quote_packet(d_open=1.0, d_close=2.0, d_high=3.0, d_low=4.0))
        assert (tick.day_open, tick.day_close, tick.day_high, tick.day_low) == (
            pytest.approx(1.0), pytest.approx(2.0), pytest.approx(3.0), pytest.approx(4.0)
        )


class TestFull:
    def test_open_interest_and_prices(self):
        tick, offset = parse_packet(full_packet())
        assert offset == 162
        assert tick.ltp == pytest.approx(125.75, abs=0.01)
        assert tick.oi == 1_234_500
        assert tick.volume == 900_000
        assert tick.day_high == pytest.approx(130.0, abs=0.01)


class TestFraming:
    def test_multiple_packets_in_one_frame(self):
        frame = ticker_packet(100.0, 1_700_000_000) + quote_packet() + full_packet()
        ticks = parse_frame(frame)
        assert len(ticks) == 3
        assert [t.security_id for t in ticks] == ["13", "1333", "35084"]

    def test_truncated_packet_stops_cleanly(self):
        frame = ticker_packet(100.0, 1_700_000_000) + quote_packet()[:20]
        ticks = parse_frame(frame)
        assert len(ticks) == 1  # the good one is kept, the partial is not guessed at

    def test_empty_frame(self):
        assert parse_frame(b"") == []

    def test_oi_packet_yields_no_tick_but_advances(self):
        packet = header(FeedCode.OI, 4, ExchangeSegment.NSE_FNO, 35084) + struct.pack("<i", 500)
        tick, offset = parse_packet(packet)
        assert tick is None
        assert offset == 12

    def test_disconnect_packet_consumed(self):
        packet = header(FeedCode.DISCONNECT, 2, ExchangeSegment.IDX_I, 0) + struct.pack("<h", 805)
        tick, offset = parse_packet(packet)
        assert tick is None
        assert offset == 10

    def test_unknown_segment_does_not_desync_the_stream(self):
        bad = struct.pack("<BHBI", FeedCode.TICKER, 8, 99, 13) + struct.pack(
            "<fi", 1.0, 1_700_000_000)
        ticks = parse_frame(bad + quote_packet())
        assert len(ticks) == 1
        assert ticks[0].security_id == "1333"


class TestTimestamps:
    def test_epoch_seconds_decode_to_ist(self):
        # 2023-11-14 22:13:20 UTC -> 03:43:20 IST next day
        tick, _ = parse_packet(ticker_packet(100.0, 1_700_000_000))
        assert tick.ts.utcoffset().total_seconds() == 5.5 * 3600

    def test_seconds_since_midnight_are_handled(self):
        """Some segments send seconds-since-midnight rather than a full epoch."""
        tick, _ = parse_packet(ticker_packet(100.0, 9 * 3600 + 20 * 60))
        assert (tick.ts.hour, tick.ts.minute) == (9, 20)
