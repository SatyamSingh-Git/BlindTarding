"""DhanHQ v2 live market feed — WebSocket client and binary packet parser.

Wire facts that the parser depends on (DhanHQ v2 spec):

* Requests are JSON text, responses are **binary little-endian**.
* Every packet starts with an 8-byte header: `<BHBI` = feed code (u8),
  message length (u16), exchange segment (u8), security id (u32).
* One WebSocket frame can carry several packets back to back, so the reader
  walks the buffer rather than assuming one packet per frame.
* Packet sizes are fixed per feed code, so we dispatch on the code and use the
  known size. The header's length field is only a fallback for codes we don't
  recognise — trusting it blindly would desynchronise the whole stream if Dhan
  ever disagrees with us about whether it includes the header.
* Server pings every 10 s and drops the connection after 40 s without a pong.
  `websockets` answers pings itself; we set the timeouts to match.

Limits: 5 connections per user, 5000 instruments each, 100 per subscribe message.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
import struct
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Sequence
from zoneinfo import ZoneInfo

import structlog
import websockets

from ..constants import (
    FEED_URL,
    IST,
    MAX_INSTRUMENTS_PER_CONNECTION,
    MAX_INSTRUMENTS_PER_SUBSCRIBE_MSG,
    ExchangeSegment,
    FeedCode,
    FeedRequestCode,
)
from ..models import Tick

log = structlog.get_logger(__name__)
IST_TZ = ZoneInfo(IST)

_HEADER = struct.Struct("<BHBI")
_HEADER_SIZE = _HEADER.size          # 8

_TICKER = struct.Struct("<fi")        # ltp, ltt
_PREV_CLOSE = struct.Struct("<fi")    # prev close, prev oi
_OI = struct.Struct("<i")
_QUOTE = struct.Struct("<fhifiiiffff")
_FULL = struct.Struct("<fhifiiiiiiffff")
_DISCONNECT = struct.Struct("<h")

# Total packet size (header included) per feed code.
_PACKET_SIZES: dict[int, int] = {
    FeedCode.TICKER: _HEADER_SIZE + _TICKER.size,          # 16
    FeedCode.PREV_CLOSE: _HEADER_SIZE + _PREV_CLOSE.size,  # 16
    FeedCode.OI: _HEADER_SIZE + _OI.size,                  # 12
    FeedCode.QUOTE: _HEADER_SIZE + _QUOTE.size,            # 50
    FeedCode.FULL: _HEADER_SIZE + _FULL.size + 100,        # 162 (incl. 5x20 depth)
    FeedCode.DISCONNECT: _HEADER_SIZE + _DISCONNECT.size,  # 10
}

_SECONDS_PER_DAY = 86_400


def _is_tradeable_price(value: float) -> bool:
    """A price must be finite and positive to be usable.

    This matters more than it looks. A NaN last-traded-price does not raise
    anywhere — it propagates into the candle, then into ATR and HMA, and every
    subsequent comparison against it silently evaluates False, so the strategy
    simply stops signalling with no error anywhere in the logs. Zero and
    negative prices come through on dead or misparsed contracts and would size
    an order off a nonsense value. Both get dropped at the edge.
    """
    return math.isfinite(value) and value > 0.0


def _clean(value: float) -> float | None:
    """Auxiliary fields are nulled rather than dropped — a bad day-high should
    not discard an otherwise good tick."""
    return float(value) if math.isfinite(value) else None


@dataclass(frozen=True, slots=True)
class FeedInstrument:
    segment: ExchangeSegment
    security_id: str

    def as_payload(self) -> dict[str, str]:
        return {"ExchangeSegment": self.segment.value, "SecurityId": str(self.security_id)}


def _decode_ltt(raw: int, *, now: dt.datetime | None = None) -> dt.datetime:
    """Last-trade-time decoder.

    Dhan sends a full epoch for most segments, but some send seconds-since-
    midnight IST. Distinguishing them by magnitude is a heuristic, not a
    guarantee — Phase 1 must verify against the Dhan chart before any signal is
    trusted, because a wrong timestamp silently corrupts every bar boundary.
    """
    now = now or dt.datetime.now(IST_TZ)
    if raw <= 0:
        return now
    if raw < _SECONDS_PER_DAY * 2:
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight + dt.timedelta(seconds=raw)
    return dt.datetime.fromtimestamp(raw, tz=dt.timezone.utc).astimezone(IST_TZ)


def parse_packet(buf: bytes | memoryview, offset: int = 0) -> tuple[Tick | None, int]:
    """Parse one packet at `offset`.

    Returns `(tick_or_None, next_offset)`. `next_offset == offset` means the
    buffer is truncated or unparseable and the caller should stop.
    """
    if offset + _HEADER_SIZE > len(buf):
        return None, offset

    code, msg_len, seg_code, security_id = _HEADER.unpack_from(buf, offset)
    size = _PACKET_SIZES.get(code)
    if size is None:
        # Unknown code: fall back to the declared length, treating it as either
        # payload-only or whole-packet, whichever keeps us inside the buffer.
        step = msg_len if msg_len >= _HEADER_SIZE else msg_len + _HEADER_SIZE
        if step <= 0 or offset + step > len(buf):
            return None, offset
        log.debug("feed.unknown_packet", code=code, msg_len=msg_len)
        return None, offset + step

    if offset + size > len(buf):
        return None, offset

    try:
        segment = ExchangeSegment.from_code(seg_code)
    except KeyError:
        log.warning("feed.unknown_segment", segment_code=seg_code, security_id=security_id)
        return None, offset + size

    sid = str(security_id)
    body = offset + _HEADER_SIZE

    if code == FeedCode.TICKER:
        ltp, ltt = _TICKER.unpack_from(buf, body)
        if not _is_tradeable_price(ltp):
            log.warning("feed.bad_price", security_id=sid, ltp=ltp, packet="ticker")
            return None, offset + size
        return Tick(sid, segment, float(ltp), _decode_ltt(ltt)), offset + size

    if code == FeedCode.QUOTE:
        (ltp, ltq, ltt, atp, volume, _sell, _buy,
         d_open, d_close, d_high, d_low) = _QUOTE.unpack_from(buf, body)
        if not _is_tradeable_price(ltp):
            log.warning("feed.bad_price", security_id=sid, ltp=ltp, packet="quote")
            return None, offset + size
        return Tick(
            sid, segment, float(ltp), _decode_ltt(ltt),
            ltq=max(0, int(ltq)), volume=max(0, int(volume)), atp=_clean(atp),
            day_open=_clean(d_open), day_high=_clean(d_high),
            day_low=_clean(d_low), day_close=_clean(d_close),
        ), offset + size

    if code == FeedCode.FULL:
        (ltp, ltq, ltt, atp, volume, _sell, _buy, oi, _oi_hi, _oi_lo,
         d_open, d_close, d_high, d_low) = _FULL.unpack_from(buf, body)
        if not _is_tradeable_price(ltp):
            log.warning("feed.bad_price", security_id=sid, ltp=ltp, packet="full")
            return None, offset + size
        return Tick(
            sid, segment, float(ltp), _decode_ltt(ltt),
            ltq=max(0, int(ltq)), volume=max(0, int(volume)), atp=_clean(atp),
            oi=max(0, int(oi)),
            day_open=_clean(d_open), day_high=_clean(d_high),
            day_low=_clean(d_low), day_close=_clean(d_close),
        ), offset + size

    if code == FeedCode.DISCONNECT:
        (reason,) = _DISCONNECT.unpack_from(buf, body)
        log.warning("feed.server_disconnect", reason_code=reason,
                    hint="805 = more than 5 sockets; oldest was dropped")
        return None, offset + size

    # OI and PREV_CLOSE carry no price — consumed, not surfaced as ticks.
    return None, offset + size


def parse_frame(payload: bytes) -> list[Tick]:
    """Walk a whole WebSocket frame, which may hold many packets."""
    ticks: list[Tick] = []
    offset = 0
    view = memoryview(payload)
    while offset < len(view):
        tick, nxt = parse_packet(view, offset)
        if nxt == offset:
            break
        if tick is not None:
            ticks.append(tick)
        offset = nxt
    return ticks


TickHandler = Callable[[Tick], Awaitable[None] | None]


class DhanFeed:
    """One WebSocket connection. Reconnects with backoff and re-subscribes.

    Data APIs do not require the static-IP proxy, so this connects directly —
    a proxy in the market-data path buys nothing and adds a failure mode.
    """

    def __init__(
        self,
        client_id: str,
        access_token: str,
        *,
        mode: FeedRequestCode = FeedRequestCode.SUBSCRIBE_QUOTE,
        on_tick: TickHandler | None = None,
        max_backoff: float = 30.0,
    ) -> None:
        self.client_id = str(client_id)
        self._token = access_token
        self.mode = mode
        self.on_tick = on_tick
        self.max_backoff = max_backoff

        self._subs: dict[tuple[str, str], FeedInstrument] = {}
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._running = False
        self._last_tick_at: dt.datetime | None = None
        self.ticks_received = 0

    @property
    def url(self) -> str:
        return (f"{FEED_URL}?version=2&token={self._token}"
                f"&clientId={self.client_id}&authType=2")

    @property
    def last_tick_at(self) -> dt.datetime | None:
        """Feed-staleness guard reads this. A live socket with no ticks during
        market hours is the dangerous case, not a dead socket."""
        return self._last_tick_at

    def seconds_since_last_tick(self) -> float | None:
        if self._last_tick_at is None:
            return None
        return (dt.datetime.now(IST_TZ) - self._last_tick_at).total_seconds()

    # --- subscriptions --------------------------------------------------
    def add(self, instruments: Iterable[FeedInstrument]) -> None:
        for inst in instruments:
            key = (inst.segment.value, str(inst.security_id))
            self._subs[key] = inst
        if len(self._subs) > MAX_INSTRUMENTS_PER_CONNECTION:
            raise ValueError(
                f"{len(self._subs)} instruments exceeds the {MAX_INSTRUMENTS_PER_CONNECTION} "
                "per-connection limit — shard across multiple DhanFeed instances"
            )

    async def _send_subscriptions(self, ws: websockets.WebSocketClientProtocol) -> None:
        instruments = list(self._subs.values())
        for batch in _batched(instruments, MAX_INSTRUMENTS_PER_SUBSCRIBE_MSG):
            await ws.send(json.dumps({
                "RequestCode": int(self.mode),
                "InstrumentCount": len(batch),
                "InstrumentList": [i.as_payload() for i in batch],
            }))
        log.info("feed.subscribed", count=len(instruments), mode=self.mode.name)

    # --- lifecycle ------------------------------------------------------
    async def run(self) -> None:
        """Connect and stream until `stop()`. Reconnects on any drop."""
        self._running = True
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    self.url, ping_interval=None, close_timeout=5, max_size=None,
                ) as ws:
                    self._ws = ws
                    log.info("feed.connected", instruments=len(self._subs))
                    await self._send_subscriptions(ws)
                    backoff = 1.0
                    await self._read_loop(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._running:
                    break
                log.warning("feed.disconnected", error=str(exc), retry_in=round(backoff, 1))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
            finally:
                self._ws = None

    async def _read_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        async for message in ws:
            if not isinstance(message, (bytes, bytearray)):
                log.debug("feed.text_message", body=str(message)[:200])
                continue
            for tick in parse_frame(bytes(message)):
                self.ticks_received += 1
                self._last_tick_at = dt.datetime.now(IST_TZ)
                if self.on_tick is None:
                    continue
                result = self.on_tick(tick)
                if asyncio.iscoroutine(result):
                    await result

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"RequestCode": int(FeedRequestCode.DISCONNECT)}))
            except Exception:
                pass
            await self._ws.close()


def _batched(items: Sequence[FeedInstrument], size: int) -> Iterable[Sequence[FeedInstrument]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


__all__ = ["DhanFeed", "FeedInstrument", "parse_packet", "parse_frame"]
