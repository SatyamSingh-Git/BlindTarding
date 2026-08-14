"""SVMKR_UT_HMA_ORB — composite strategy.

**The name is misleading, and this is the single most important fact about the
port.** Tracing the Pine source, `buy` and `sell` depend only on the UT Bot
trailing stop:

    buy  = src > xATRTrailingStop and crossover(ema(src,1), xATRTrailingStop)
    sell = src < xATRTrailingStop and crossover(xATRTrailingStop, ema(src,1))

`n1` (the Hull MA) appears only in a colour expression and a `plot()`.
`orb_high` / `orb_low` appear only in their own computation and two `plot()`
calls. Neither is referenced by `buy`, `sell`, or either `alertcondition`. So
every Buy/Sell label on the chart, and every alert the script can fire, comes
from the UT Bot alone — the Hull MA and the opening range are visual context.

Both are still computed here and exposed as *optional* filters
(`require_hma_agreement`, `require_orb_breakout`), defaulting to **off** so that
the default behaviour reproduces the chart exactly. Turning either on is a
deliberate deviation from the indicator, not a bug fix, and the validation gate
must be re-run afterwards.

Fidelity is chosen over speed throughout: every evaluation recomputes the full
warm window rather than carrying incremental state. A few hundred bars of ATR
and WMA costs microseconds, and incremental state is exactly where a drift
against the chart would hide.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Sequence

from ..models import Action, Candle, Signal
from .indicators import pine
from .indicators.hma import hma_with_slope
from .indicators.orb import ORBResult, opening_range
from .indicators.pine import Series
from .indicators.utbot import UTBotResult, ut_bot

# Values read off the live chart legend "3 1 31 5 1010-1015" and the Inputs
# dialog. Note these are NOT the input() defaults in the Pine source — the
# source defaults UT Key Value to 2, the chart runs 3.
CHART_DEFAULTS = {
    "ut_key": 3.0,
    "ut_atr_period": 1,
    "use_heikin_ashi": False,
    "hma_length": 31,
    "orb_minutes": 5,
    "orb_session": "1010-1015",
}


@dataclass(slots=True)
class StrategyParams:
    ut_key: float = 3.0
    ut_atr_period: int = 1
    use_heikin_ashi: bool = False
    hma_length: int = 31
    orb_minutes: int = 5
    orb_session: str = "1010-1015"

    # Deviations from the indicator. Off by default — see the module docstring.
    require_hma_agreement: bool = False
    require_orb_breakout: bool = False

    def __post_init__(self) -> None:
        if self.ut_atr_period < 1:
            raise ValueError("ut_atr_period must be at least 1")
        if self.hma_length < 1:
            raise ValueError("hma_length must be at least 1")
        if self.ut_key <= 0:
            raise ValueError("ut_key must be positive")

    @property
    def min_bars(self) -> int:
        """Bars of history needed before the first signal can be trusted.

        The HMA is the binding constraint: it needs `hma_length` bars for its
        inner WMA plus `round(sqrt(n))` for the outer one. Tripled, because
        Wilder smoothing keeps drifting toward its true value well past the
        nominal window — starting cold produces an ATR that is wrong for
        dozens of bars, and therefore a stop that is wrong too.
        """
        base = max(self.hma_length + int(self.hma_length ** 0.5) + 2,
                   self.ut_atr_period + 2)
        return max(60, base * 3)


@dataclass(slots=True)
class StrategyResult:
    timestamps: list[dt.datetime]
    closes: list[float]
    ut: UTBotResult
    hma: Series
    hma_prev: Series
    hma_rising: list[bool]
    orb: ORBResult
    buy: list[bool]          # after optional filters
    sell: list[bool]
    raw_buy: list[bool]      # exactly what the Pine source would plot
    raw_sell: list[bool]
    filtered: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.timestamps)


class SvmkrUtHmaOrb:
    """Evaluates the indicator over a window of closed candles."""

    name = "svmkr_ut_hma_orb"

    def __init__(self, params: StrategyParams | None = None, *, plan: str = "") -> None:
        self.params = params or StrategyParams()
        self.plan = plan

    # --- core ------------------------------------------------------------
    def evaluate(self, candles: Sequence[Candle]) -> StrategyResult:
        p = self.params
        if not candles:
            empty_ut = UTBotResult([], [], [], [], [], [])
            return StrategyResult([], [], empty_ut, [], [], [],
                                  ORBResult([], [], [], []), [], [], [], [])

        timestamps = [c.ts for c in candles]
        opens = [c.open for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        closes = [c.close for c in candles]

        source = closes
        if p.use_heikin_ashi:
            _, ha_high, ha_low, ha_close = pine.heikin_ashi(opens, highs, lows, closes)
            # The source swaps only the signal series, not the ATR inputs:
            # `atr(c)` still reads the real candles.
            source = ha_close

        ut = ut_bot(highs, lows, closes,
                    key_value=p.ut_key, atr_period=p.ut_atr_period, source=source)
        hma_now, hma_prev, hma_rising = hma_with_slope(closes, p.hma_length)
        orb = opening_range(timestamps, highs, lows, p.orb_session)

        raw_buy, raw_sell = list(ut.buy), list(ut.sell)
        buy, sell, filtered = self._apply_filters(
            raw_buy, raw_sell, closes, hma_rising, orb)

        return StrategyResult(
            timestamps=timestamps, closes=closes, ut=ut,
            hma=hma_now, hma_prev=hma_prev, hma_rising=hma_rising, orb=orb,
            buy=buy, sell=sell, raw_buy=raw_buy, raw_sell=raw_sell,
            filtered=filtered,
        )

    def _apply_filters(
        self,
        raw_buy: list[bool],
        raw_sell: list[bool],
        closes: list[float],
        hma_rising: list[bool],
        orb: ORBResult,
    ) -> tuple[list[bool], list[bool], list[str]]:
        p = self.params
        if not (p.require_hma_agreement or p.require_orb_breakout):
            return raw_buy, raw_sell, []

        buy, sell, notes = list(raw_buy), list(raw_sell), []
        for i in range(len(closes)):
            if p.require_hma_agreement:
                if buy[i] and not hma_rising[i]:
                    buy[i] = False
                    notes.append(f"{i}:buy blocked, HMA falling")
                if sell[i] and hma_rising[i]:
                    sell[i] = False
                    notes.append(f"{i}:sell blocked, HMA rising")
            if p.require_orb_breakout:
                high_level, low_level = orb.high[i], orb.low[i]
                # A None level means the session has not happened yet today.
                # Treat that as "no breakout confirmed" rather than passing.
                if buy[i] and (high_level is None or closes[i] <= high_level):
                    buy[i] = False
                    notes.append(f"{i}:buy blocked, not above ORB high")
                if sell[i] and (low_level is None or closes[i] >= low_level):
                    sell[i] = False
                    notes.append(f"{i}:sell blocked, not below ORB low")
        return buy, sell, notes

    # --- signal extraction -----------------------------------------------
    def signals(self, candles: Sequence[Candle]) -> list[Signal]:
        """Every signal in the window. Used by replay and the validation gate."""
        result = self.evaluate(candles)
        out: list[Signal] = []
        for i in range(len(result)):
            action = (Action.BUY if result.buy[i]
                      else Action.SELL if result.sell[i] else None)
            if action is None:
                continue
            out.append(Signal(
                plan=self.plan, action=action,
                bar_ts=result.timestamps[i], price=result.closes[i],
                source="engine",
                reason=f"ut_bot {'crossover' if action == Action.BUY else 'crossunder'}",
                meta={
                    "stop": result.ut.stop[i],
                    "atr": result.ut.atr[i],
                    "hma": result.hma[i],
                    "hma_rising": result.hma_rising[i],
                    "orb_high": result.orb.high[i],
                    "orb_low": result.orb.low[i],
                },
            ))
        return out

    def latest_signal(self, candles: Sequence[Candle]) -> Signal | None:
        """Signal on the most recent bar only — the live path.

        Returns `None` if there is not enough history. Emitting from a cold
        start would fire on a stop computed from a half-warmed ATR.
        """
        if len(candles) < self.params.min_bars:
            return None
        result = self.evaluate(candles)
        last = len(result) - 1
        if last < 0:
            return None
        action = (Action.BUY if result.buy[last]
                  else Action.SELL if result.sell[last] else None)
        if action is None:
            return None
        return Signal(
            plan=self.plan, action=action,
            bar_ts=result.timestamps[last], price=result.closes[last],
            source="engine",
            reason=f"ut_bot {'crossover' if action == Action.BUY else 'crossunder'}",
            meta={"stop": result.ut.stop[last], "atr": result.ut.atr[last]},
        )


def params_from_config(raw: dict) -> StrategyParams:
    """Build params from a plan's `strategy.params` block, falling back to the
    values read off the live chart rather than the Pine `input()` defaults."""
    merged = {**CHART_DEFAULTS, **(raw or {})}
    known = {f for f in StrategyParams.__slots__}
    return StrategyParams(**{k: v for k, v in merged.items() if k in known})


__all__ = ["SvmkrUtHmaOrb", "StrategyParams", "StrategyResult",
           "params_from_config", "CHART_DEFAULTS"]
