"""Angel One SmartAPI WebSocket 2.0 feed.

Wire facts, taken from Angel's official SDK rather than prose docs:

* Little-endian binary. Every packet starts with a fixed 43-byte header:
  mode (u8), exchange type (u8), token (25-byte null-terminated **string**),
  sequence number (i64), exchange timestamp (i64 milliseconds).
* LTP mode is 51 bytes, Quote 123, SnapQuote 379.
* **Every price is an int64 in paise.** Dividing by 100 is not cosmetic — an
  unscaled price is 100x wrong, and a 100x wrong price does not raise anywhere.
  It sizes an order, moves a stop, and looks entirely plausible in a log.
  `PriceScaleCheck` cross-checks the feed against a REST snapshot at startup
  precisely because being wrong here is silent and expensive.
* The client sends the literal text `ping` every 10 seconds; Angel does not
  drive the heartbeat.
* Authentication needs **both** the REST jwt and the separate feed token.

The token in a packet is a bare id with no segment: exchange type 1 covers NSE
cash *and* indices. A tick's real segment is therefore recovered from what we
subscribed, not from the packet — guessing would mislabel every index tick.
"""

from __future__ import annotations

import asyncio
import random
import time
import datetime as dt
import json
import struct
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable
from zoneinfo import ZoneInfo

import structlog
import websockets

from ...constants import IST, ExchangeSegment
from ...models import Tick
from .auth import AngelAuth
from .instruments import exchange_type_code

log = structlog.get_logger(__name__)
IST_TZ = ZoneInfo(IST)

FEED_URL = "wss://smartapisocket.angelone.in/smart-stream"
HEARTBEAT_MESSAGE = "ping"
HEARTBEAT_INTERVAL = 10

SUBSCRIBE_ACTION = 1
UNSUBSCRIBE_ACTION = 0

MODE_LTP = 1
MODE_QUOTE = 2
MODE_SNAP_QUOTE = 3

PACKET_SIZES = {MODE_LTP: 51, MODE_QUOTE: 123, MODE_SNAP_QUOTE: 379}

# Angel transmits every price as an integer number of paise.
PAISE = 100.0

_HEADER = struct.Struct("<BB25sqq")      # mode, exchange, token, seq, ts  = 43 bytes
_LTP = struct.Struct("<q")               # 43-51
_QUOTE_TAIL = struct.Struct("<qqqddqqqq")  # 51-123
_MAX_TOKENS_PER_MESSAGE = 1000


# A connection has to survive this long before it counts as healthy enough to
# reset the reconnect backoff.
STABLE_SECONDS = 60.0
# Where the backoff starts once Angel has answered with 429. Retrying faster is
# what earned the 429 in the first place.
THROTTLED_BACKOFF = 60.0


@dataclass(frozen=True, slots=True)
class AngelFeedInstrument:
    segment: ExchangeSegment
    security_id: str


def _price(raw: int) -> float:
    return raw / PAISE


# Epoch milliseconds outside this range are not a real exchange timestamp.
# `datetime.fromtimestamp` raises OSError on absurd values rather than returning
# something odd, and an uncaught raise in the parser kills the feed reader — so
# one malformed packet would take the whole feed down mid-session.
_MIN_TS_MS = 946_684_800_000        # 2000-01-01
_MAX_TS_MS = 4_102_444_800_000      # 2100-01-01


def _decode_timestamp(ts_ms: int) -> dt.datetime:
    if not (_MIN_TS_MS <= ts_ms <= _MAX_TS_MS):
        return dt.datetime.now(IST_TZ)
    try:
        return dt.datetime.fromtimestamp(
            ts_ms / 1000, tz=dt.timezone.utc).astimezone(IST_TZ)
    except (OSError, OverflowError, ValueError):
        return dt.datetime.now(IST_TZ)


def parse_packet(buf: bytes | memoryview, offset: int = 0,
                 *, segment_lookup: Callable[[str, int], ExchangeSegment] | None = None
                 ) -> tuple[Tick | None, int]:
    """Parse one packet. Returns `(tick_or_None, next_offset)`.

    `next_offset == offset` means the buffer is truncated and the caller must
    stop rather than guess at a boundary.
    """
    if offset + _HEADER.size > len(buf):
        return None, offset

    mode, exchange_type, token_raw, _seq, ts_ms = _HEADER.unpack_from(buf, offset)
    size = PACKET_SIZES.get(mode)
    if size is None or offset + size > len(buf):
        return None, offset

    token = token_raw.split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()
    if not token:
        return None, offset + size

    (ltp_raw,) = _LTP.unpack_from(buf, offset + _HEADER.size)
    ltp = _price(ltp_raw)
    if not (ltp > 0):
        # A zero or negative price is a dead or misparsed contract. Letting it
        # through would poison the candle and every indicator built on it.
        log.warning("angel.bad_price", token=token, raw=ltp_raw, mode=mode)
        return None, offset + size

    segment = (segment_lookup(token, exchange_type) if segment_lookup
               else ExchangeSegment.NSE_EQ)
    ts = _decode_timestamp(ts_ms)

    if mode == MODE_LTP:
        return Tick(token, segment, ltp, ts), offset + size

    (ltq, atp, volume, _buy_qty, _sell_qty,
     day_open, day_high, day_low, day_close) = _QUOTE_TAIL.unpack_from(
        buf, offset + 51)

    oi = None
    if mode == MODE_SNAP_QUOTE:
        (oi,) = struct.unpack_from("<q", buf, offset + 131)

    return Tick(
        token, segment, ltp, ts,
        ltq=max(0, int(ltq)), volume=max(0, int(volume)),
        atp=_price(atp) or None,
        oi=max(0, int(oi)) if oi is not None else None,
        day_open=_price(day_open) or None, day_high=_price(day_high) or None,
        day_low=_price(day_low) or None, day_close=_price(day_close) or None,
    ), offset + size


def parse_frame(payload: bytes, *,
                segment_lookup: Callable[[str, int], ExchangeSegment] | None = None
                ) -> list[Tick]:
    ticks: list[Tick] = []
    offset, view = 0, memoryview(payload)
    while offset < len(view):
        tick, nxt = parse_packet(view, offset, segment_lookup=segment_lookup)
        if nxt == offset:
            break
        if tick is not None:
            ticks.append(tick)
        offset = nxt
    return ticks


TickHandler = Callable[[Tick], Awaitable[None] | None]


class AngelFeed:
    """One SmartAPI WebSocket connection, reconnecting with backoff."""

    def __init__(
        self,
        auth: AngelAuth,
        *,
        mode: int = MODE_QUOTE,
        on_tick: TickHandler | None = None,
        max_backoff: float = 120.0,
    ) -> None:
        self.auth = auth
        self.mode = mode
        self.on_tick = on_tick
        self.max_backoff = max_backoff
        self._subs: dict[str, AngelFeedInstrument] = {}
        self._ws = None
        self._running = False
        self._last_tick_at: dt.datetime | None = None
        self.ticks_received = 0
        # When the socket last went down. A bar built while the feed was
        # dropping is missing ticks, and a missing high or low gives a wrong
        # ATR, a wrong stop, and a crossing that never happened -- four such
        # phantom signals cost Rs 14,090 on 18 August.
        self._last_disconnect_at: dt.datetime | None = None
        self.disconnects = 0

    # --- subscriptions ---------------------------------------------------
    def add(self, instruments: Iterable[AngelFeedInstrument]) -> None:
        for inst in instruments:
            self._subs[str(inst.security_id)] = inst

    def _segment_for(self, token: str, exchange_type: int) -> ExchangeSegment:
        """Recover a tick's segment from what we subscribed.

        Exchange type 1 covers both NSE cash and indices, so the packet alone
        cannot distinguish them — an index tick would be labelled as equity and
        never match its plan.
        """
        known = self._subs.get(token)
        if known is not None:
            return known.segment
        from .instruments import segment_for_code
        return segment_for_code(exchange_type)

    def _token_list(self) -> list[dict]:
        grouped: dict[int, list[str]] = {}
        for inst in self._subs.values():
            grouped.setdefault(exchange_type_code(inst.segment), []).append(
                str(inst.security_id))
        return [{"exchangeType": code, "tokens": tokens[:_MAX_TOKENS_PER_MESSAGE]}
                for code, tokens in grouped.items()]

    async def _subscribe(self, ws) -> None:
        token_list = self._token_list()
        if not token_list:
            return
        await ws.send(json.dumps({
            "correlationID": "orbot",
            "action": SUBSCRIBE_ACTION,
            "params": {"mode": self.mode, "tokenList": token_list},
        }))
        log.info("angel_feed.subscribed", groups=len(token_list),
                 instruments=len(self._subs), mode=self.mode)

    # --- health ----------------------------------------------------------
    @property
    def last_tick_at(self) -> dt.datetime | None:
        return self._last_tick_at

    @property
    def last_disconnect_at(self) -> dt.datetime | None:
        return self._last_disconnect_at

    def dropped_since(self, when: dt.datetime) -> bool:
        """Did the connection go down at any point since `when`?"""
        if self._last_disconnect_at is None:
            return False
        return self._last_disconnect_at >= when

    def seconds_since_last_tick(self) -> float | None:
        if self._last_tick_at is None:
            return None
        return (dt.datetime.now(IST_TZ) - self._last_tick_at).total_seconds()

    # --- lifecycle -------------------------------------------------------
    async def run(self) -> None:
        self._running = True
        backoff = 1.0
        while self._running:
            connected_at: float | None = None
            try:
                headers = {
                    "Authorization": self.auth.jwt(),
                    "x-api-key": self.auth.api_key,
                    "x-client-code": self.auth.client_code,
                    "x-feed-token": self.auth.feed_token(),
                }
                async with websockets.connect(
                    FEED_URL, additional_headers=headers,
                    ping_interval=None, close_timeout=5, max_size=None,
                ) as ws:
                    self._ws = ws
                    log.info("angel_feed.connected", instruments=len(self._subs))
                    await self._subscribe(ws)
                    connected_at = time.monotonic()
                    heartbeat = asyncio.create_task(self._heartbeat(ws))
                    try:
                        await self._read_loop(ws)
                    finally:
                        heartbeat.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._running:
                    break
                # Reset the backoff only for a connection that actually held.
                # Resetting on *connect* meant a socket that died a second later
                # retried at 1s forever, and Angel answered a day of that with
                # HTTP 429 -- so the reconnect storm became the outage.
                lived = (time.monotonic() - connected_at) if connected_at else 0.0
                if lived >= STABLE_SECONDS:
                    backoff = 1.0

                text = str(exc)
                throttled = "429" in text or "too many" in text.lower()
                if throttled:
                    # Being rate limited is not a transient blip: retrying
                    # sooner is what caused it. Start well past the usual floor.
                    backoff = max(backoff, THROTTLED_BACKOFF)

                # Jitter, so a restart storm across plans does not resynchronise
                # into a thundering herd on the same second.
                wait = backoff * (0.7 + random.random() * 0.6)
                self._last_disconnect_at = dt.datetime.now(IST_TZ)
                self.disconnects += 1
                log.warning("angel_feed.disconnected", error=text[:200],
                            retry_in=round(wait, 1), lived_for=round(lived, 1),
                            throttled=throttled)
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, self.max_backoff)
            finally:
                self._ws = None

    async def _heartbeat(self, ws) -> None:
        """Angel does not drive the heartbeat — the client must, or the socket
        is dropped as idle."""
        while self._running:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                await ws.send(HEARTBEAT_MESSAGE)
            except Exception:
                return

    async def _read_loop(self, ws) -> None:
        async for message in ws:
            if not isinstance(message, (bytes, bytearray)):
                text = str(message).strip()
                if text and text.lower() not in ("pong", "ping"):
                    log.debug("angel_feed.text", body=text[:200])
                continue
            for tick in parse_frame(bytes(message),
                                    segment_lookup=self._segment_for):
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
                await self._ws.close()
            except Exception:
                pass


class PriceScaleCheck:
    """Cross-checks feed prices against a REST snapshot.

    Exists because the paise divisor is the one number in this adapter where
    being wrong is both catastrophic and completely silent. A 100x error would
    not raise; it would size orders, trip stops and log plausible-looking
    values. Documentation is not enough assurance for that, so the scaling is
    verified against an independent source before any trading begins.
    """

    TOLERANCE = 0.02       # 2% — generous, since the two reads are moments apart

    def __init__(self) -> None:
        self.checked = False
        self.ok = False
        self.detail = ""

    def verify(self, feed_price: float, rest_price: float | None) -> bool:
        self.checked = True
        if not rest_price or rest_price <= 0 or feed_price <= 0:
            self.ok = False
            self.detail = "no reference price available to compare against"
            return False
        ratio = feed_price / rest_price
        self.ok = abs(ratio - 1.0) <= self.TOLERANCE
        if self.ok:
            self.detail = f"feed {feed_price:,.2f} vs REST {rest_price:,.2f} — agree"
        else:
            hint = ""
            if 0.005 < ratio < 0.02:
                hint = " — feed looks 100x too small; paise scaling applied twice"
            elif 50 < ratio < 200:
                hint = " — feed looks 100x too large; paise scaling not applied"
            self.detail = (f"feed {feed_price:,.2f} vs REST {rest_price:,.2f}, "
                           f"ratio {ratio:.4f}{hint}")
        return self.ok


__all__ = ["AngelFeed", "AngelFeedInstrument", "PriceScaleCheck",
           "parse_packet", "parse_frame", "MODE_LTP", "MODE_QUOTE",
           "MODE_SNAP_QUOTE", "PACKET_SIZES", "PAISE", "FEED_URL"]
