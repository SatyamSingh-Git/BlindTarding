"""UT Bot — ATR trailing stop.

This is the *entire* tradeable signal of SVMKR_UT_HMA_ORB. The Hull MA and the
opening range are plotted but never referenced by `buy` or `sell` in the Pine
source, so everything that fires an alert originates here.

Ported literally from the v4 source, including the parts that are redundant.
`ema(src, 1)` is mathematically the source itself, and `buy = src > stop and
crossover(ema, stop)` collapses to just `crossover(src, stop)` — but the literal
form is kept so the code can be diffed against the Pine line by line. Cleverness
here buys nothing and risks a silent divergence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from . import pine
from .pine import Series


@dataclass(slots=True)
class UTBotResult:
    atr: Series
    stop: Series
    buy: list[bool]
    sell: list[bool]
    position: list[int]          # +1 long, -1 short, 0 undecided
    long_bias: list[bool] = field(default_factory=list)   # src above the stop


def ut_bot(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    *,
    key_value: float,
    atr_period: int,
    source: Sequence[float] | None = None,
) -> UTBotResult:
    """Compute the trailing stop and its crossover signals.

    `source` defaults to close; pass Heikin Ashi closes to mirror the
    "Signals from Heikin Ashi Candles" input.
    """
    src: list[float] = list(source) if source is not None else list(closes)
    n = len(src)
    if n == 0:
        return UTBotResult([], [], [], [], [], [])

    xatr = pine.atr(highs, lows, closes, atr_period)
    n_loss: Series = [None if v is None else key_value * v for v in xatr]

    stop: Series = []
    for i in range(n):
        prev_stop = pine.nz(stop[i - 1] if i > 0 else None, 0.0)
        loss = n_loss[i]
        if loss is None:
            # ATR still warming up. Pine propagates na through the arithmetic.
            stop.append(None)
            continue

        prev_src = src[i - 1] if i > 0 else None
        cur = src[i]

        if cur > prev_stop and pine.gt(prev_src, prev_stop):
            stop.append(max(prev_stop, cur - loss))
        elif cur < prev_stop and (prev_src is not None and prev_src < prev_stop):
            stop.append(min(prev_stop, cur + loss))
        elif cur > prev_stop:
            stop.append(cur - loss)
        else:
            stop.append(cur + loss)

    # `pos` exists in the source but only drives an unused colour variable. It is
    # computed anyway because it is the cleanest read on current directional state.
    position: list[int] = []
    for i in range(n):
        prev_stop = pine.nz(stop[i - 1] if i > 0 else None, 0.0)
        prev_src = src[i - 1] if i > 0 else None
        carried = position[i - 1] if i > 0 else 0
        if prev_src is not None and prev_src < prev_stop and src[i] > prev_stop:
            position.append(1)
        elif prev_src is not None and prev_src > prev_stop and src[i] < prev_stop:
            position.append(-1)
        else:
            position.append(carried)

    ema1 = pine.ema(src, 1)                 # identity at length 1, kept literal
    above = pine.crossover(ema1, stop)
    below = pine.crossover(stop, ema1)

    buy = [pine.gt(src[i], stop[i]) and above[i] for i in range(n)]
    sell = [(stop[i] is not None and src[i] < stop[i]) and below[i] for i in range(n)]
    long_bias = [pine.gt(src[i], stop[i]) for i in range(n)]

    return UTBotResult(atr=xatr, stop=stop, buy=buy, sell=sell,
                       position=position, long_bias=long_bias)


__all__ = ["ut_bot", "UTBotResult"]
