"""Opening range breakout levels.

Plot-only in this strategy — `orb_high` and `orb_low` never appear in `buy` or
`sell`. Ported so the levels are available as an optional filter and so the
chart can be reproduced exactly.

The configured session is **10:10–10:15**, not the market open. That is worth
stating plainly because "ORB" almost always implies the opening bell, and the
strategy would behave completely differently if it did. On a 5-minute chart the
window spans exactly one bar; on a 1-minute chart, five.

Pine session semantics reproduced here: a bar is in-session when its *opening*
time falls in `[start, end)`. The end is exclusive, so a 10:15 bar is outside a
`1010-1015` window.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Sequence

from .pine import Series


@dataclass(slots=True)
class ORBResult:
    high: Series
    low: Series
    in_session: list[bool]
    is_first: list[bool]


def parse_session(spec: str) -> tuple[dt.time, dt.time]:
    """Parse Pine's session string, e.g. `"1010-1015"`.

    A trailing day mask (`":1234567"`) is accepted and ignored — the configured
    mask covers every day of the week.
    """
    spec = spec.strip().split(":")[0]
    start_raw, _, end_raw = spec.partition("-")
    if not start_raw or not end_raw:
        raise ValueError(f"malformed session spec {spec!r}, expected 'HHMM-HHMM'")

    def to_time(raw: str) -> dt.time:
        raw = raw.strip()
        if len(raw) != 4 or not raw.isdigit():
            raise ValueError(f"malformed session time {raw!r}, expected HHMM")
        hour, minute = int(raw[:2]), int(raw[2:])
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(f"session time out of range: {raw}")
        return dt.time(hour, minute)

    return to_time(start_raw), to_time(end_raw)


def in_session_flags(timestamps: Sequence[dt.datetime], start: dt.time,
                     end: dt.time) -> list[bool]:
    """`[start, end)` on the bar's opening time.

    A session that wraps past midnight is supported for completeness, though NSE
    equity and F&O sessions never do.
    """
    flags: list[bool] = []
    wraps = start > end
    for ts in timestamps:
        clock = ts.time()
        flags.append(clock >= start or clock < end if wraps
                     else start <= clock < end)
    return flags


def opening_range(
    timestamps: Sequence[dt.datetime],
    highs: Sequence[float],
    lows: Sequence[float],
    session: str,
) -> ORBResult:
    """Track the high and low of each day's session window.

    The levels carry forward for the rest of the session and reset when a new
    session begins. Before the first session of the series they are `None`,
    matching Pine's `float(na)` initialisation — and a `None` level must never
    be treated as a breakout level.
    """
    start, end = parse_session(session)
    in_sess = in_session_flags(timestamps, start, end)

    is_first: list[bool] = []
    highs_out: Series = []
    lows_out: Series = []

    for i in range(len(timestamps)):
        first = in_sess[i] and not (in_sess[i - 1] if i > 0 else False)
        is_first.append(first)

        if first:
            cur_high: float | None = highs[i]
            cur_low: float | None = lows[i]
        else:
            cur_high = highs_out[i - 1] if i > 0 else None
            cur_low = lows_out[i - 1] if i > 0 else None

        # Pine: `high > na` is false, so levels stay na until a session starts.
        if in_sess[i] and cur_high is not None and highs[i] > cur_high:
            cur_high = highs[i]
        if in_sess[i] and cur_low is not None and lows[i] < cur_low:
            cur_low = lows[i]

        highs_out.append(cur_high)
        lows_out.append(cur_low)

    return ORBResult(high=highs_out, low=lows_out,
                     in_session=in_sess, is_first=is_first)


__all__ = ["opening_range", "parse_session", "in_session_flags", "ORBResult"]
