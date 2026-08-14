"""Turn signals into trades, and trades into numbers.

Two measures, kept firmly apart because conflating them would be misleading:

**Index points** — what the signal captured on the instrument the strategy
actually watches. This is a measure of *signal quality*. It is not your P&L and
must never be presented as rupees.

**Modelled option P&L** — for plans that execute options, the resolved ATM
contract's actual historical premium at entry and exit, times the lot. This is
much closer to real, and it is usually far worse than the index points suggest:
a 50-point index move is not a 50-point premium move, theta bleeds while you
hold, and the strike you get is not the strike you would have picked with
hindsight.

Both are modelled from bar closes. Real fills are worse — spread, slippage,
brokerage and taxes are not here. Treat the output as an upper bound.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from ..constants import IST, InstrumentType, OptionType
from ..models import Action

log = structlog.get_logger(__name__)
IST_TZ = ZoneInfo(IST)


@dataclass(slots=True)
class Trade:
    entry_ts: dt.datetime
    entry_price: float
    side: str = "long"
    exit_ts: dt.datetime | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    # option leg, when modelled
    contract: str = ""
    entry_premium: float | None = None
    exit_premium: float | None = None
    lot_size: int = 0
    quantity: int = 0

    def close(self, ts: dt.datetime, price: float, reason: str) -> None:
        self.exit_ts, self.exit_price, self.exit_reason = ts, price, reason

    @property
    def points(self) -> float | None:
        """Signed by side: a short profits when price falls."""
        if self.exit_price is None:
            return None
        move = self.exit_price - self.entry_price
        return move if self.side == "long" else -move

    @property
    def option_pnl(self) -> float | None:
        if self.entry_premium is None or self.exit_premium is None:
            return None
        return (self.exit_premium - self.entry_premium) * self.quantity

    @property
    def is_open(self) -> bool:
        return self.exit_price is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "entry_ts": self.entry_ts.isoformat(),
            "entry_price": self.entry_price,
            "exit_ts": self.exit_ts.isoformat() if self.exit_ts else None,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "points": self.points,
            "contract": self.contract or None,
            "entry_premium": self.entry_premium,
            "exit_premium": self.exit_premium,
            "quantity": self.quantity or None,
            "option_pnl": self.option_pnl,
            "open": self.is_open,
        }


def pair_trades(result, *, square_off: dt.time, max_trades_per_day: int = 0,
                direction: str = "long", start: dt.datetime | None = None
                ) -> list[Trade]:
    """Pair signals into trades.

    `direction` decides which side is taken:

    * ``long``  — BUY opens, SELL closes. The configured behaviour.
    * ``short`` — SELL opens a short, BUY closes it.
    * ``both``  — always in the market: every signal closes one side and opens
      the other. Two transactions per signal, which is what "sell twice, buy
      twice" means in practice.

    Two guards are applied because they materially change which trades exist,
    and a backtest that ignored them would describe a system nobody is running:
    no entry at or after the square-off cutoff, and anything still open at the
    cutoff is closed there rather than carried overnight.
    """
    trades: list[Trade] = []
    open_trade: Trade | None = None
    per_day: dict[dt.date, int] = {}
    want_long = direction in ("long", "both")
    want_short = direction in ("short", "both")

    for i, ts in enumerate(result.timestamps):
        if start is not None and ts < start:
            continue
        day = ts.date()
        past_cutoff = ts.time() >= square_off

        if open_trade is not None and past_cutoff:
            open_trade.close(ts, result.closes[i], "square-off")
            trades.append(open_trade)
            open_trade = None

        fired = "buy" if result.buy[i] else "sell" if result.sell[i] else None
        if fired is None:
            continue

        # Close whatever is open — this is the first of the two transactions.
        if open_trade is not None:
            closes_it = ((fired == "sell" and open_trade.side == "long")
                         or (fired == "buy" and open_trade.side == "short"))
            if closes_it:
                open_trade.close(ts, result.closes[i], "signal")
                trades.append(open_trade)
                open_trade = None

        if past_cutoff or open_trade is not None:
            continue

        side = "long" if fired == "buy" else "short"
        if side == "long" and not want_long:
            continue
        if side == "short" and not want_short:
            continue
        if max_trades_per_day and per_day.get(day, 0) >= max_trades_per_day:
            continue
        per_day[day] = per_day.get(day, 0) + 1
        open_trade = Trade(entry_ts=ts, entry_price=result.closes[i], side=side)

    if open_trade is not None:
        trades.append(open_trade)
    return trades


def summarise(trades: list[Trade], *, key: str = "points") -> dict[str, Any]:
    """Stats over closed trades. `key` is 'points' or 'option_pnl'."""
    values = [getattr(t, key) for t in trades if getattr(t, key) is not None]
    if not values:
        return {"trades": 0, "available": False}

    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    total = sum(values)

    equity, peak, drawdown = 0.0, 0.0, 0.0
    curve = []
    for t in trades:
        v = getattr(t, key)
        if v is None:
            continue
        equity += v
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        curve.append({"ts": (t.exit_ts or t.entry_ts).isoformat(),
                      "equity": round(equity, 2), "value": round(v, 2)})

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "available": True,
        "trades": len(values),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(values) * 100,
        "total": total,
        "avg": total / len(values),
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
        "best": max(values),
        "worst": min(values),
        "profit_factor": (gross_win / gross_loss) if gross_loss else None,
        "max_drawdown": drawdown,
        "curve": curve,
    }


def model_option_leg(trades: list[Trade], plan, registry, client, *,
                     limit: int = 60) -> tuple[int, list[str]]:
    """Fill in the option premium each trade would actually have paid.

    For every entry, resolve the contract the live resolver would have chosen
    at that moment — same strike rule, same expiry rule — then read that
    contract's own historical bars for the entry and exit premium.

    Costs one historical request per trade, so it is capped and opt-in.
    """
    from ..execution.resolver import ExecutionResolver
    from ..models import Signal

    notes: list[str] = []
    cfg = plan.execution
    if cfg.mode != "option" or not cfg.on_buy or not cfg.on_buy.option_type:
        notes.append("plan does not execute options — index points only")
        return 0, notes

    resolver = ExecutionResolver(registry, plan)
    itype = InstrumentType.OPTIDX
    filled = 0
    cache: dict[tuple[str, dt.date], dict[dt.datetime, float]] = {}

    for trade in trades[-limit:]:
        if trade.exit_ts is None:
            continue
        try:
            expiry = registry.select_expiry(cfg.underlying, itype,
                                            cfg.on_buy.expiry,
                                            today=trade.entry_ts.date())
            contract = resolver._resolve_option(cfg.on_buy, trade.entry_price,
                                                today=trade.entry_ts.date())
        except Exception as exc:
            notes.append(f"{trade.entry_ts:%d %b %H:%M}: no contract ({exc})")
            continue

        key = (contract.security_id, trade.entry_ts.date())
        bars = cache.get(key)
        if bars is None:
            try:
                day = trade.entry_ts.date()
                fetched = client.intraday(
                    contract, plan.signal.tf.minutes if plan.signal.tf.is_intraday else 5,
                    dt.datetime.combine(day, dt.time(9, 0)),
                    dt.datetime.combine(day, dt.time(15, 40)))
            except Exception as exc:
                notes.append(f"{contract.display_name}: no history ({exc})")
                cache[key] = {}
                continue
            bars = {c.ts.replace(tzinfo=IST_TZ) if c.ts.tzinfo is None else c.ts: c.close
                    for c in fetched}
            cache[key] = bars
        if not bars:
            continue

        entry_p = bars.get(trade.entry_ts)
        exit_p = bars.get(trade.exit_ts)
        if entry_p is None or exit_p is None:
            notes.append(f"{contract.display_name}: premium missing at "
                         f"{trade.entry_ts:%H:%M} or {trade.exit_ts:%H:%M}")
            continue

        trade.contract = contract.display_name
        trade.entry_premium = entry_p
        trade.exit_premium = exit_p
        trade.lot_size = contract.lot_size
        trade.quantity = contract.lot_size * max(1, cfg.qty_lots)
        filled += 1

    if filled and len(trades) > limit:
        notes.append(f"modelled the most recent {limit} trades only")
    return filled, notes


PERIODS = {
    "today": 1, "2d": 2, "3d": 3, "1w": 7, "2w": 14, "1m": 30, "3m": 90,
    "all": None,
}


def _period_start(period: str, timestamps: list[dt.datetime]) -> dt.datetime | None:
    """Resolve a period to a cut-off, counted in *trading sessions* rather than
    calendar days — "last 3 days" should mean three sessions, not a window that
    quietly contains a weekend."""
    sessions = PERIODS.get(period)
    if not sessions or not timestamps:
        return None
    days = sorted({t.date() for t in timestamps})
    if len(days) <= sessions:
        return None
    cutoff = days[-sessions]
    return next(t for t in timestamps if t.date() == cutoff)


def performance_payload(plan_name: str, *, bars: int = 1500,
                        model_options: bool = False,
                        period: str = "all",
                        direction: str = "long") -> dict[str, Any]:
    from ..broker.factory import build_broker
    from ..config import get_settings, load_plans
    from ..engine.history import fetch_candles
    from ..engine.strategy import SvmkrUtHmaOrb, params_from_config

    settings = get_settings()
    plan = next((p for p in load_plans() if p.name == plan_name), None)
    if plan is None:
        return {"error": f"no plan named {plan_name!r}"}

    bundle = build_broker(settings, refresh_instruments=False)
    inst = bundle.registry.get(plan.signal.exchange_segment, plan.signal.security_id)
    tf = plan.signal.tf
    params = params_from_config(plan.strategy.params)

    end = dt.datetime.now()
    span = max(6, int(bars / max(tf.approx_bars_per_session, 0.1)) + 4)
    candles = fetch_candles(bundle.client, inst, tf,
                            end - dt.timedelta(days=int(span * 1.6) + 10), end)
    if not candles:
        return {"error": "no candles returned"}

    result = SvmkrUtHmaOrb(params, plan=plan.name).evaluate(candles)
    start = _period_start(period, list(result.timestamps))

    def build(dirn: str) -> list[Trade]:
        return pair_trades(result, square_off=plan.risk.square_off_time,
                           max_trades_per_day=plan.risk.max_trades_per_day,
                           direction=dirn, start=start)

    trades = build(direction)
    # Always compute all three sides. The question "would trading both
    # directions help?" is only answerable side by side, and it is cheap.
    sides = {d: summarise(build(d), key="points") for d in ("long", "short", "both")}

    notes: list[str] = []
    modelled = 0
    if model_options:
        modelled, notes = model_option_leg(trades, plan, bundle.registry,
                                           bundle.client)

    window = [t for t in result.timestamps if start is None or t >= start]
    sessions = len({t.date() for t in window})
    return {
        "period": period,
        "direction": direction,
        "sides": sides,
        "periods": list(PERIODS),
        "plan": plan.name,
        "instrument": inst.display_name,
        "timeframe": tf.label,
        "sessions": sessions,
        "from": window[0].isoformat() if window else None,
        "to": window[-1].isoformat() if window else None,
        "square_off": plan.risk.square_off_time.strftime("%H:%M"),
        "max_trades_per_day": plan.risk.max_trades_per_day,
        "points": summarise(trades, key="points"),
        "options": summarise(trades, key="option_pnl") if modelled else
                   {"available": False, "trades": 0},
        "modelled": modelled,
        "notes": notes,
        "trades": [t.to_dict() for t in trades[-80:]],
    }


__all__ = ["performance_payload", "pair_trades", "summarise", "Trade"]
