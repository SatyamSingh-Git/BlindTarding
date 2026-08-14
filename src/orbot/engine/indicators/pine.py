"""Pine Script v4 primitives, reimplemented exactly.

Every function here exists because the obvious Python equivalent is subtly
different from what Pine actually computes. Those differences do not raise —
they shift a value by a fraction, which moves a crossover by a bar, which moves
an entry. The whole port's fidelity rests on this file.

The known traps, all of which are handled below:

* **`round()` is not Python's `round()`.** Pine rounds half away from zero;
  Python uses banker's rounding. `round(14.5)` is 15 in Pine and 14 in Python.
  This feeds directly into the HMA's window lengths.
* **`atr()` is RMA, not SMA.** Wilder's smoothing, seeded with an SMA of the
  first `length` values. `pandas.rolling().mean()` is a different indicator.
* **`na` is not `NaN`.** Any comparison against `na` in Pine is false, whereas
  NumPy propagates NaN and Python raises on `None`. Represented here as `None`
  with explicit comparison helpers.
* **Series are indexed backwards in Pine.** `close[1]` is the *previous* bar.
  These functions take forward-ordered lists (oldest first) and return
  forward-ordered lists, so `series[i-1]` is the previous bar.
"""

from __future__ import annotations

import math
from typing import Sequence

Series = list[float | None]


def pine_round(value: float) -> int:
    """Pine's `round()` — half away from zero.

    Python's built-in rounds half to even, so `round(14.5)` disagrees: Pine
    gives 15, Python gives 14. The HMA uses `round(n/2)` and `round(sqrt(n))`
    for its window lengths, so this discrepancy silently changes which bars are
    averaged.
    """
    if value >= 0:
        return int(math.floor(value + 0.5))
    return int(math.ceil(value - 0.5))


def nz(value: float | None, replacement: float = 0.0) -> float:
    """Pine's `nz()` — substitute for `na`."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return replacement
    return value


def gt(a: float | None, b: float | None) -> bool:
    """`a > b` with Pine's na semantics: any comparison involving na is false."""
    if a is None or b is None:
        return False
    return a > b


def lte(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return False
    return a <= b


def sma(source: Sequence[float | None], length: int) -> Series:
    """Simple moving average. `na` until `length` bars are available."""
    out: Series = []
    running = 0.0
    window: list[float] = []
    for value in source:
        if value is None:
            out.append(None)
            continue
        window.append(value)
        running += value
        if len(window) > length:
            running -= window.pop(0)
        out.append(running / length if len(window) == length else None)
    return out


def wma(source: Sequence[float | None], length: int) -> Series:
    """Weighted moving average — weights `length, length-1, ..., 1`, most recent
    bar weighted heaviest. The HMA is built from three of these."""
    if length <= 0:
        raise ValueError("wma length must be positive")
    out: Series = []
    norm = length * (length + 1) / 2
    window: list[float] = []
    for value in source:
        if value is None:
            out.append(None)
            window.clear()
            continue
        window.append(value)
        if len(window) > length:
            window.pop(0)
        if len(window) < length:
            out.append(None)
            continue
        total = 0.0
        for offset, item in enumerate(window):
            total += item * (offset + 1)   # oldest gets weight 1, newest `length`
        out.append(total / norm)
    return out


def rma(source: Sequence[float | None], length: int) -> Series:
    """Wilder's smoothing (Pine's `rma`), seeded with an SMA of the first
    `length` values.

    `alpha = 1/length`, so at `length == 1` this is the identity — which is
    exactly the configuration this strategy runs (`UT ATR Period = 1`), making
    ATR equal to the raw true range of each bar.
    """
    if length <= 0:
        raise ValueError("rma length must be positive")
    out: Series = []
    alpha = 1.0 / length
    prev: float | None = None
    seed: list[float] = []
    for value in source:
        if value is None:
            out.append(None)
            continue
        if prev is None:
            seed.append(value)
            if len(seed) < length:
                out.append(None)
                continue
            prev = sum(seed) / length      # SMA seed, per Pine
            out.append(prev)
            continue
        prev = alpha * value + (1 - alpha) * prev
        out.append(prev)
    return out


def ema(source: Sequence[float | None], length: int) -> Series:
    """Pine's `ema`. At `length == 1` alpha is 1, so it returns the source
    unchanged — which is why `ema(src, 1)` in the strategy is a no-op."""
    if length <= 0:
        raise ValueError("ema length must be positive")
    out: Series = []
    alpha = 2.0 / (length + 1)
    prev: float | None = None
    for value in source:
        if value is None:
            out.append(None)
            continue
        prev = value if prev is None else alpha * value + (1 - alpha) * prev
        out.append(prev)
    return out


def true_range(highs: Sequence[float], lows: Sequence[float],
               closes: Sequence[float]) -> Series:
    """`max(high-low, |high-close[1]|, |low-close[1]|)`.

    On the first bar there is no previous close, so Pine falls back to
    `high - low`.
    """
    out: Series = []
    for i in range(len(highs)):
        if i == 0:
            out.append(highs[i] - lows[i])
            continue
        prev_close = closes[i - 1]
        out.append(max(
            highs[i] - lows[i],
            abs(highs[i] - prev_close),
            abs(lows[i] - prev_close),
        ))
    return out


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        length: int) -> Series:
    """Pine's `atr(length)` = `rma(true_range, length)`."""
    return rma(true_range(highs, lows, closes), length)


def crossover(a: Sequence[float | None], b: Sequence[float | None]) -> list[bool]:
    """Pine's `crossover(a, b)`: true on the bar where `a` rises above `b`.

    Definition is `a > b and a[1] <= b[1]` — note the previous bar uses `<=`,
    not `<`, so a touch followed by a break counts as a cross.
    """
    out = [False]
    for i in range(1, len(a)):
        out.append(gt(a[i], b[i]) and lte(a[i - 1], b[i - 1]))
    return out


def change(source: Sequence[float | None]) -> Series:
    """Pine's `change()` — difference from the previous bar."""
    out: Series = [None]
    for i in range(1, len(source)):
        cur, prev = source[i], source[i - 1]
        out.append(None if cur is None or prev is None else cur - prev)
    return out


def heikin_ashi(opens: Sequence[float], highs: Sequence[float],
                lows: Sequence[float], closes: Sequence[float]
                ) -> tuple[list[float], list[float], list[float], list[float]]:
    """Heikin Ashi candles.

    Unused at the current settings (`Signals from Heikin Ashi Candles` is off),
    but the input exists and flipping it changes every signal, so it is ported
    rather than left as a landmine.
    """
    ha_close: list[float] = []
    ha_open: list[float] = []
    ha_high: list[float] = []
    ha_low: list[float] = []
    for i in range(len(closes)):
        close_i = (opens[i] + highs[i] + lows[i] + closes[i]) / 4
        open_i = (opens[i] + closes[i]) / 2 if i == 0 else (ha_open[i - 1] + ha_close[i - 1]) / 2
        ha_close.append(close_i)
        ha_open.append(open_i)
        ha_high.append(max(highs[i], open_i, close_i))
        ha_low.append(min(lows[i], open_i, close_i))
    return ha_open, ha_high, ha_low, ha_close


__all__ = [
    "Series", "pine_round", "nz", "gt", "lte",
    "sma", "wma", "rma", "ema", "true_range", "atr",
    "crossover", "change", "heikin_ashi",
]
