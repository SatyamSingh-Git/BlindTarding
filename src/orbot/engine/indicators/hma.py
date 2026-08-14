"""Hull Moving Average, as written in the SVMKR source.

Plot-only in this strategy — `n1` feeds a colour and a `plot()` call and nothing
else. It is ported because it is the natural trend filter to switch on if the
raw UT Bot signal turns out to be too noisy, and because getting it wrong later
is harder than getting it right now.

Two fidelity points:

* The window lengths come from Pine's `round()`, which rounds half away from
  zero. At the configured length of 31 this happens to agree with Python
  (`round(15.5) == 16`), but at length 29 it would not — Pine yields 15 and
  Python yields 14, changing which bars are averaged.
* The source computes a second HMA over `close[1]` purely to compare against the
  first. That is the same series shifted one bar, so `n2[i] == n1[i-1]` and the
  colour rule reduces to "is the HMA rising". Both are exposed.
"""

from __future__ import annotations

import math
from typing import Sequence

from . import pine
from .pine import Series


def hma(source: Sequence[float | None], length: int) -> Series:
    """`wma(2*wma(src, n/2) - wma(src, n), sqrt(n))`."""
    if length <= 0:
        raise ValueError("hma length must be positive")
    half = pine.pine_round(length / 2)
    sqn = pine.pine_round(math.sqrt(length))

    half_wma = pine.wma(source, half)
    full_wma = pine.wma(source, length)
    diff: Series = [
        None if (a is None or b is None) else 2 * a - b
        for a, b in zip(half_wma, full_wma)
    ]
    return pine.wma(diff, sqn)


def hma_with_slope(source: Sequence[float | None], length: int
                   ) -> tuple[Series, Series, list[bool]]:
    """Return `(n1, n2, rising)` matching the source's variable names.

    `n2` is `n1` delayed by one bar — the source builds it from `close[1]`,
    which is the same thing. `rising` is the green/red colour rule.
    """
    n1 = hma(source, length)
    n2: Series = [None] + n1[:-1] if n1 else []
    rising = [pine.gt(n1[i], n2[i]) for i in range(len(n1))]
    return n1, n2, rising


__all__ = ["hma", "hma_with_slope"]
