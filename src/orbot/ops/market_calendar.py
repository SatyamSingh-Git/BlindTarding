"""NSE trading calendar and session windows.

A deliberate design point: **the calendar is a convenience, not the safety
mechanism.** Exchange holiday lists change (new holidays get added, muhurat
sessions move, sudden closures happen), and a stale hardcoded list that says
"open" on a closed day is exactly the kind of quiet wrongness that gets orders
sent into a void.

So the real protection against trading when the market is shut is the
feed-staleness guard: no ticks means no trading, regardless of what any calendar
claims. This module exists to avoid pointless work and to give clear log
messages, and its holiday list is user-maintained in `config/calendar.yaml`.

Weekends and clock times are certain and are treated as such. Holidays are
advisory and default to empty rather than to a guess — an empty list makes the
system rely on the feed, which is the safe failure mode. A wrong list would not.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog
import yaml

from ..constants import IST

log = structlog.get_logger(__name__)
IST_TZ = ZoneInfo(IST)

NSE_OPEN = dt.time(9, 15)
NSE_CLOSE = dt.time(15, 30)
# Pre-open auction runs 09:00-09:08 with order matching to 09:15. Prices tick
# during it, but they are auction prices, not continuous trading — signalling
# off them would be wrong.
NSE_PREOPEN_START = dt.time(9, 0)


@dataclass(slots=True)
class MarketCalendar:
    open_time: dt.time = NSE_OPEN
    close_time: dt.time = NSE_CLOSE
    holidays: set[dt.date] = field(default_factory=set)
    special_sessions: dict[dt.date, tuple[dt.time, dt.time]] = field(default_factory=dict)
    tz: ZoneInfo = IST_TZ

    @classmethod
    def load(cls, path: Path | None = None) -> MarketCalendar:
        if path is None or not path.exists():
            log.info("calendar.no_file",
                     hint="no holiday list configured; relying on the feed-staleness "
                          "guard to detect closures")
            return cls()
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        holidays = {_as_date(d) for d in (raw.get("holidays") or [])}
        holidays.discard(None)

        specials: dict[dt.date, tuple[dt.time, dt.time]] = {}
        for entry in raw.get("special_sessions") or []:
            day = _as_date(entry.get("date"))
            if day is None:
                continue
            specials[day] = (_as_time(entry.get("open"), NSE_OPEN),
                             _as_time(entry.get("close"), NSE_CLOSE))

        return cls(
            open_time=_as_time(raw.get("open"), NSE_OPEN),
            close_time=_as_time(raw.get("close"), NSE_CLOSE),
            holidays=holidays,  # type: ignore[arg-type]
            special_sessions=specials,
        )

    # --- queries ---------------------------------------------------------
    def session_window(self, day: dt.date) -> tuple[dt.time, dt.time]:
        return self.special_sessions.get(day, (self.open_time, self.close_time))

    def is_trading_day(self, day: dt.date) -> bool:
        if day in self.special_sessions:
            return True          # muhurat and the like override a weekend/holiday
        if day.weekday() >= 5:   # Saturday, Sunday
            return False
        return day not in self.holidays

    def is_open(self, when: dt.datetime | None = None) -> bool:
        when = self._localise(when)
        if not self.is_trading_day(when.date()):
            return False
        start, end = self.session_window(when.date())
        return start <= when.time() < end

    def is_preopen(self, when: dt.datetime | None = None) -> bool:
        when = self._localise(when)
        if not self.is_trading_day(when.date()):
            return False
        start, _ = self.session_window(when.date())
        return NSE_PREOPEN_START <= when.time() < start

    def minutes_to_close(self, when: dt.datetime | None = None) -> float:
        """Negative once the session has ended. Used by the square-off logic."""
        when = self._localise(when)
        _, end = self.session_window(when.date())
        close_at = dt.datetime.combine(when.date(), end, tzinfo=self.tz)
        return (close_at - when).total_seconds() / 60

    def next_trading_day(self, day: dt.date, *, limit: int = 30) -> dt.date | None:
        for offset in range(1, limit + 1):
            candidate = day + dt.timedelta(days=offset)
            if self.is_trading_day(candidate):
                return candidate
        return None

    def describe(self, when: dt.datetime | None = None) -> str:
        when = self._localise(when)
        if not self.is_trading_day(when.date()):
            reason = "weekend" if when.weekday() >= 5 else "holiday"
            return f"closed ({reason})"
        start, end = self.session_window(when.date())
        if self.is_preopen(when):
            return f"pre-open auction, continuous trading from {start:%H:%M}"
        if when.time() < start:
            return f"before open, session {start:%H:%M}-{end:%H:%M}"
        if when.time() >= end:
            return f"after close (session ended {end:%H:%M})"
        return f"open, {self.minutes_to_close(when):.0f} min to close"

    def _localise(self, when: dt.datetime | None) -> dt.datetime:
        if when is None:
            return dt.datetime.now(self.tz)
        if when.tzinfo is None:
            return when.replace(tzinfo=self.tz)
        return when.astimezone(self.tz)


def _as_date(value: object) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value.strip())
        except ValueError:
            log.warning("calendar.bad_date", value=value)
    return None


def _as_time(value: object, default: dt.time) -> dt.time:
    if isinstance(value, dt.time):
        return value
    if isinstance(value, str):
        try:
            return dt.time.fromisoformat(value.strip())
        except ValueError:
            log.warning("calendar.bad_time", value=value)
    return default


__all__ = ["MarketCalendar", "NSE_OPEN", "NSE_CLOSE", "NSE_PREOPEN_START"]
