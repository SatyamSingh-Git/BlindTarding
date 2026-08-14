"""Timeframe handling — 5 minutes to 2 days.

Dhan serves only five intraday intervals natively (1, 5, 15, 25 and 60 minutes)
plus a separate daily endpoint. Everything else has to be built by resampling a
smaller interval, and resampling is where timeframes go quietly wrong.

**Native intervals can be validated cleanly against the chart. Resampled ones
carry anchoring risk** and must be validated separately, because two systems can
disagree about where a bar starts without either being obviously broken. That
distinction is exposed as `is_native` so the tooling can warn rather than
pretend everything is equivalent.

The specific hazard for NSE: a session is 09:15–15:30, which is 375 minutes.
That divides evenly by 5, 15 and 25 but **not** by 30 (12.5 bars) or 60
(6.25 bars). So a naive "group every N bars" resampler eventually produces a bar
that starts before the close on one day and ends after the open on the next.
Bars are therefore bucketed by session-anchored wall-clock time, never by count.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..constants import INTRADAY_INTERVALS

# The user-facing range: nothing faster than 5m, nothing slower than 2 days.
MIN_INTRADAY_MINUTES = 5
MAX_DAYS = 2

_PATTERN = re.compile(r"^\s*(\d+)\s*([a-zA-Z]*)\s*$")

_UNIT_ALIASES = {
    "": "m", "m": "m", "min": "m", "mins": "m", "minute": "m", "minutes": "m",
    "h": "h", "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
    "d": "d", "day": "d", "days": "d",
}


class TimeframeError(ValueError):
    """Raised for a timeframe we cannot serve. Never silently substitute a
    neighbouring interval — that would trade a different chart than intended."""


@dataclass(frozen=True, slots=True)
class Timeframe:
    label: str
    minutes: int | None      # intraday bar size; None for daily and above
    days: int | None         # daily multiple; None for intraday

    # --- parsing ---------------------------------------------------------
    @classmethod
    def parse(cls, spec: str | int) -> Timeframe:
        if isinstance(spec, Timeframe):
            return spec
        if isinstance(spec, int):
            return cls._intraday(spec)

        match = _PATTERN.match(str(spec))
        if not match:
            raise TimeframeError(
                f"cannot parse timeframe {spec!r} — expected e.g. '5m', '1h', '1D'"
            )
        amount = int(match.group(1))
        unit = _UNIT_ALIASES.get(match.group(2).lower())
        if unit is None:
            raise TimeframeError(f"unknown timeframe unit in {spec!r}")
        if amount <= 0:
            raise TimeframeError(f"timeframe must be positive, got {spec!r}")

        if unit == "d":
            if amount > MAX_DAYS:
                raise TimeframeError(
                    f"{amount}D exceeds the {MAX_DAYS}-day maximum for this system"
                )
            return cls(label=f"{amount}D", minutes=None, days=amount)
        return cls._intraday(amount * 60 if unit == "h" else amount)

    @classmethod
    def _intraday(cls, minutes: int) -> Timeframe:
        if minutes < MIN_INTRADAY_MINUTES:
            raise TimeframeError(
                f"{minutes}m is below the {MIN_INTRADAY_MINUTES}-minute minimum"
            )
        if minutes % 60 == 0 and minutes >= 60:
            label = f"{minutes // 60}h"
        else:
            label = f"{minutes}m"
        return cls(label=label, minutes=minutes, days=None)

    # --- properties ------------------------------------------------------
    @property
    def is_intraday(self) -> bool:
        return self.minutes is not None

    @property
    def is_native(self) -> bool:
        """True when Dhan serves this interval directly.

        Native intervals are the ones whose bar boundaries come from the
        exchange's own aggregation rather than ours, so they are the ones the
        validation gate can trust without a separate anchoring check.
        """
        if not self.is_intraday:
            return self.days == 1
        return self.minutes in INTRADAY_INTERVALS

    @property
    def base_interval(self) -> int | None:
        """Dhan intraday interval to fetch. `None` means use the daily endpoint."""
        if not self.is_intraday:
            return None
        if self.minutes in INTRADAY_INTERVALS:
            return self.minutes
        # Largest native interval that divides this one evenly.
        divisors = [i for i in INTRADAY_INTERVALS
                    if i >= MIN_INTRADAY_MINUTES and self.minutes % i == 0]
        if not divisors:
            raise TimeframeError(
                f"{self.label} cannot be built from Dhan's native intervals "
                f"{INTRADAY_INTERVALS} — pick one of those instead"
            )
        return max(divisors)

    @property
    def resample_factor(self) -> int:
        """How many base bars make one bar at this timeframe."""
        if not self.is_intraday:
            return self.days or 1
        base = self.base_interval
        return 1 if base is None else self.minutes // base

    @property
    def needs_resampling(self) -> bool:
        return self.resample_factor > 1

    @property
    def approx_bars_per_session(self) -> float:
        """NSE equity/F&O session is 375 minutes."""
        if not self.is_intraday:
            return 1 / (self.days or 1)
        return 375 / self.minutes

    def __str__(self) -> str:
        return self.label


def supported() -> list[Timeframe]:
    """Every timeframe offered, cheapest to build first.

    Deliberately a fixed menu rather than "anything that parses": an arbitrary
    interval like 7m would resample from 1m with boundaries that match nothing
    on any chart.
    """
    specs = ["5m", "10m", "15m", "20m", "25m", "30m", "45m",
             "1h", "2h", "3h", "4h", "1D", "2D"]
    return [Timeframe.parse(s) for s in specs]


def describe_support() -> list[tuple[str, str, str]]:
    """`(label, source, caveat)` rows for the CLI."""
    rows: list[tuple[str, str, str]] = []
    for tf in supported():
        if tf.is_native and tf.is_intraday:
            rows.append((tf.label, f"Dhan native {tf.minutes}m", "validated directly"))
        elif tf.is_native:
            rows.append((tf.label, "Dhan daily endpoint", "validated directly"))
        elif tf.is_intraday:
            rows.append((tf.label, f"{tf.base_interval}m x {tf.resample_factor}",
                         "resampled — verify bar alignment against the chart"))
        else:
            rows.append((tf.label, f"daily x {tf.resample_factor}",
                         "resampled — multi-day anchoring may differ from TradingView"))
    return rows


__all__ = ["Timeframe", "TimeframeError", "supported", "describe_support",
           "MIN_INTRADAY_MINUTES", "MAX_DAYS"]
