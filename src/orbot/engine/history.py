"""Historical candle fetching at an arbitrary timeframe.

Sits between the broker client and the strategy: works out which native Dhan
interval to request, fetches it, and aggregates up to the requested timeframe.
Kept out of `broker/` so the client stays a thin transport layer with no opinion
about bar construction.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

import structlog

from ..models import Candle, Instrument
from .candles import resample_to, session_start_for
from .timeframe import Timeframe

if TYPE_CHECKING:  # avoids a broker <-> engine import cycle at runtime
    from ..broker.dhan_client import DhanClient

log = structlog.get_logger(__name__)

# NSE trades roughly 21 sessions a month; pad the calendar window so holidays
# and long weekends cannot leave the indicator short of warmup bars.
_CALENDAR_PADDING = 1.55


def fetch_candles(
    client: DhanClient,
    instrument: Instrument,
    timeframe: Timeframe,
    start: dt.datetime,
    end: dt.datetime,
    *,
    session_start: dt.time | None = None,
) -> list[Candle]:
    """Fetch bars at `timeframe`, resampling from a native interval if needed."""
    if timeframe.is_intraday:
        base = client.intraday(instrument, timeframe.base_interval, start, end)
    else:
        base = client.daily(instrument, start.date(), end.date())

    if not timeframe.needs_resampling:
        return base

    out = resample_to(base, timeframe,
                      session_start=session_start
                      or session_start_for(instrument.segment))
    log.info("history.resampled", timeframe=timeframe.label,
             base_interval=timeframe.base_interval, base_bars=len(base),
             out_bars=len(out))
    return out


def lookback_for(timeframe: Timeframe, bars_needed: int) -> dt.timedelta:
    """Calendar span likely to contain `bars_needed` bars at this timeframe.

    Deliberately generous. Fetching too much history costs a little bandwidth;
    fetching too little means the strategy silently refuses to signal, or worse,
    signals off a half-warmed ATR.
    """
    if timeframe.is_intraday:
        sessions = bars_needed / max(timeframe.approx_bars_per_session, 0.01)
    else:
        sessions = bars_needed * (timeframe.days or 1)
    calendar_days = max(2.0, sessions * (7 / 5) * _CALENDAR_PADDING)
    return dt.timedelta(days=calendar_days)


def warmup_candles(
    client: DhanClient,
    instrument: Instrument,
    timeframe: Timeframe,
    bars_needed: int,
    *,
    now: dt.datetime | None = None,
    max_attempts: int = 3,
) -> list[Candle]:
    """Fetch at least `bars_needed` bars, widening the window if short.

    A fixed lookback is not enough on its own: a run of holidays, a newly listed
    contract, or an illiquid strike can all return fewer bars than the calendar
    span suggests. Each retry doubles the window rather than failing quietly with
    an under-warmed indicator.
    """
    now = now or dt.datetime.now()
    span = lookback_for(timeframe, bars_needed)

    candles: list[Candle] = []
    for attempt in range(max_attempts):
        candles = fetch_candles(client, instrument, timeframe, now - span, now)
        if len(candles) >= bars_needed:
            return candles
        log.warning("history.short_warmup", timeframe=timeframe.label,
                    got=len(candles), needed=bars_needed,
                    days=round(span.days), attempt=attempt + 1)
        span *= 2
    return candles


__all__ = ["fetch_candles", "lookback_for", "warmup_candles"]
