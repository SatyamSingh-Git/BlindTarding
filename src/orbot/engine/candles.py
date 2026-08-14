"""Tick to OHLCV aggregation.

Three details here decide whether our bars match the chart, and all three are
easy to get subtly wrong:

**Bar alignment is to the session open, not to midnight.** TradingView anchors
intraday bars at the session start. NSE opens 09:15, so 5-minute bars run
09:15-09:20, and 60-minute bars run 09:15-10:15 — *not* 09:00-10:00. For 5m and
15m the distinction happens to vanish (09:15 is 555 minutes, divisible by both),
but for 25m and 60m it does not, and anchoring to midnight would shift every bar.

**Volume comes from cumulative deltas.** The Quote packet carries the running
day volume, not per-trade size. Per-bar volume is the difference between the
last cumulative reading in this bar and the last one in the previous bar. Adding
up `ltq` instead would multiply-count every quote refresh.

**Bars close on the clock, not on the next tick.** An illiquid strike can go a
whole bar without printing. `flush(now)` closes bars whose window has elapsed so
the strategy fires on time; without it a signal waits for the next trade, which
may be minutes late. We do not synthesise empty bars — TradingView doesn't
either, and inventing flat bars would corrupt ATR and HMA.
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from typing import Callable, Iterable, Sequence
from zoneinfo import ZoneInfo

from ..constants import IST
from ..models import Candle, Tick
from .timeframe import Timeframe

IST_TZ = ZoneInfo(IST)
NSE_SESSION_START = dt.time(9, 15)
MCX_SESSION_START = dt.time(9, 0)


class CandleSeries:
    """Rolling OHLCV for one instrument at one timeframe."""

    def __init__(
        self,
        timeframe_minutes: int,
        *,
        session_start: dt.time = NSE_SESSION_START,
        max_bars: int = 5000,
        tz: ZoneInfo = IST_TZ,
    ) -> None:
        if timeframe_minutes <= 0:
            raise ValueError("timeframe_minutes must be positive")
        self.timeframe = dt.timedelta(minutes=timeframe_minutes)
        self.timeframe_minutes = timeframe_minutes
        self.session_start = session_start
        self.tz = tz
        self.bars: deque[Candle] = deque(maxlen=max_bars)
        self.current: Candle | None = None
        self._cum_volume: int | None = None      # running day volume, last reading
        self._bar_start_volume: int | None = None  # cumulative volume when bar opened

    # --- bar boundaries -------------------------------------------------
    def bar_start(self, ts: dt.datetime) -> dt.datetime:
        """Floor `ts` to its bar, anchored at the session open."""
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=self.tz)
        anchor = ts.astimezone(self.tz).replace(
            hour=self.session_start.hour, minute=self.session_start.minute,
            second=0, microsecond=0,
        )
        if ts < anchor:
            # Pre-open activity belongs to the previous session's grid.
            anchor -= dt.timedelta(days=1)
        elapsed = ts - anchor
        periods = int(elapsed // self.timeframe)
        return anchor + periods * self.timeframe

    def bar_end(self, bar_ts: dt.datetime) -> dt.datetime:
        return bar_ts + self.timeframe

    # --- warmup ---------------------------------------------------------
    def seed(self, candles: Iterable[Candle]) -> int:
        """Load historical bars so indicators start warm. Live aggregation
        resumes from the last seeded bar; a bar already seeded is never
        rebuilt from ticks."""
        count = 0
        for candle in candles:
            candle.closed = True
            if self.bars and candle.ts <= self.bars[-1].ts:
                continue
            self.bars.append(candle)
            count += 1
        self.current = None
        self._cum_volume = None
        self._bar_start_volume = None
        return count

    # --- live aggregation -----------------------------------------------
    def on_tick(self, tick: Tick) -> Candle | None:
        """Fold a tick in. Returns the previous bar if this tick closed it."""
        ts = tick.ts if tick.ts.tzinfo else tick.ts.replace(tzinfo=self.tz)
        start = self.bar_start(ts)

        if self.bars and start < self.bars[-1].ts:
            return None  # late tick for an already-closed bar; ignore

        volume_delta = self._volume_delta(tick)
        closed: Candle | None = None

        if self.current is None:
            self.current = Candle.opening(start, tick.ltp, volume_delta)
            self._bar_start_volume = self._cum_volume
        elif start > self.current.ts:
            closed = self._close_current()
            self.current = Candle.opening(start, tick.ltp, volume_delta)
            self._bar_start_volume = self._cum_volume
        else:
            self.current.update(tick.ltp, volume_delta)

        if tick.oi is not None and self.current is not None:
            self.current.oi = tick.oi
        return closed

    def flush(self, now: dt.datetime | None = None) -> Candle | None:
        """Close the working bar if its window has elapsed. Call this on a timer
        so signals fire at bar close even when nothing traded."""
        if self.current is None:
            return None
        now = now or dt.datetime.now(self.tz)
        if now.tzinfo is None:
            now = now.replace(tzinfo=self.tz)
        if now < self.bar_end(self.current.ts):
            return None
        return self._close_current()

    def _close_current(self) -> Candle | None:
        candle = self.current
        if candle is None:
            return None
        candle.closed = True
        self.bars.append(candle)
        self.current = None
        return candle

    def _volume_delta(self, tick: Tick) -> int:
        """Convert cumulative day volume into per-bar volume."""
        if tick.volume is None:
            return 0
        cumulative = int(tick.volume)
        previous = self._cum_volume
        self._cum_volume = cumulative
        if previous is None:
            return 0          # first reading only establishes the baseline
        if cumulative < previous:
            return 0          # counter reset (new session / reconnect)
        return cumulative - previous

    # --- access ---------------------------------------------------------
    def closes(self, n: int | None = None) -> list[float]:
        bars = list(self.bars)[-n:] if n else list(self.bars)
        return [b.close for b in bars]

    def last_closed(self) -> Candle | None:
        return self.bars[-1] if self.bars else None

    def __len__(self) -> int:
        return len(self.bars)


BarHandler = Callable[[tuple[str, str], Candle], None]


class CandleBook:
    """Fans ticks out to one `CandleSeries` per (instrument, timeframe)."""

    def __init__(self, on_bar_close: BarHandler | None = None) -> None:
        self._series: dict[tuple[str, str, int], CandleSeries] = {}
        self.on_bar_close = on_bar_close

    def track(
        self,
        key: tuple[str, str],
        timeframe_minutes: int,
        *,
        session_start: dt.time = NSE_SESSION_START,
    ) -> CandleSeries:
        ident = (key[0], key[1], timeframe_minutes)
        series = self._series.get(ident)
        if series is None:
            series = CandleSeries(timeframe_minutes, session_start=session_start)
            self._series[ident] = series
        return series

    def get(self, key: tuple[str, str], timeframe_minutes: int) -> CandleSeries | None:
        return self._series.get((key[0], key[1], timeframe_minutes))

    def on_tick(self, tick: Tick) -> None:
        for (seg, sid, _tf), series in self._series.items():
            if (seg, sid) != tick.key:
                continue
            closed = series.on_tick(tick)
            if closed is not None and self.on_bar_close is not None:
                self.on_bar_close(tick.key, closed)

    def flush(self, now: dt.datetime | None = None) -> None:
        for (seg, sid, _tf), series in self._series.items():
            closed = series.flush(now)
            if closed is not None and self.on_bar_close is not None:
                self.on_bar_close((seg, sid), closed)

    def __len__(self) -> int:
        return len(self._series)


def _merge(ts: dt.datetime, group: Sequence[Candle]) -> Candle:
    return Candle(
        ts=ts,
        open=group[0].open,
        high=max(c.high for c in group),
        low=min(c.low for c in group),
        close=group[-1].close,
        volume=sum(c.volume for c in group),
        oi=group[-1].oi,
        closed=True,
    )


def resample_to(
    candles: Sequence[Candle],
    target: Timeframe,
    *,
    session_start: dt.time = NSE_SESSION_START,
) -> list[Candle]:
    """Aggregate bars up to a larger timeframe.

    Bucketed by **session-anchored wall-clock time**, never by counting bars.
    The distinction is not academic: an NSE session is 375 minutes, which
    divides evenly by 5, 15 and 25 but not by 30 (12.5 bars) or 60 (6.25 bars).
    Grouping every N bars would therefore drift, and eventually emit a bar
    opening before one day's close and closing after the next day's open — a bar
    that exists on no chart anywhere and spans an overnight gap.

    Daily and above group whole trading days, so holidays and half-days shift
    the grid rather than tearing it.
    """
    if not candles:
        return []
    ordered = sorted(candles, key=lambda c: c.ts)

    # Insertion order is chronological because `ordered` is sorted, so the
    # buckets come out in order without a second sort.
    groups: list[list[Candle]] = []
    stamps: list[dt.datetime] = []
    seen: dict[object, int] = {}

    if target.is_intraday:
        grid = CandleSeries(target.minutes, session_start=session_start)
        keys = [grid.bar_start(c.ts) for c in ordered]
        stamp_for = lambda i, c: keys[i]          # noqa: E731 - bar opens on the grid
    else:
        span = target.days or 1
        days = sorted({c.ts.date() for c in ordered})
        index = {day: i for i, day in enumerate(days)}
        keys = [index[c.ts.date()] // span for c in ordered]
        stamp_for = lambda i, c: c.ts             # noqa: E731 - first day in the group

    for i, candle in enumerate(ordered):
        key = keys[i]
        slot = seen.get(key)
        if slot is None:
            seen[key] = len(groups)
            groups.append([candle])
            stamps.append(stamp_for(i, candle))
        else:
            groups[slot].append(candle)

    return [_merge(stamps[i], group) for i, group in enumerate(groups)]


__all__ = ["CandleSeries", "CandleBook", "resample_to",
           "NSE_SESSION_START", "MCX_SESSION_START"]
