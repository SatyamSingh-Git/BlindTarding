"""The risk guard layer.

Every signal passes all of these before an order exists, and every order passes
the rest before it is sent. This is the difference between "blind" execution and
"unattended" execution — the strategy decides *what* to trade, the guards decide
whether trading at all is safe right now.

Three design rules:

**Fail closed.** Any guard that cannot evaluate — a funds call that errors, a
reconciliation that cannot reach the broker — blocks. An unknown state is not a
safe state.

**Blocking is cheap, a bad fill is not.** A missed trade costs one signal. A
duplicate order, a trade on a frozen feed, or a position the bot has lost track
of can cost far more, and none of them announce themselves.

**Halts are sticky.** A tripped daily-loss limit stays tripped until a human
clears it. A limit that un-trips when P&L bounces back is not a limit, and a
crash-loop that resets it becomes an unlimited-loss loop.

Guards are ordered cheapest-and-most-decisive first, so the common rejection
path costs a file stat and a dictionary lookup.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Callable
from zoneinfo import ZoneInfo

import structlog

from ..config import GlobalRisk, TradePlan
from ..constants import IST, OPTION_TYPES, InstrumentType, TransactionType
from ..models import OrderIntent, Position, Signal
from ..ops.journal import Journal
from ..ops.killswitch import KillSwitch
from ..ops.market_calendar import MarketCalendar

log = structlog.get_logger(__name__)
IST_TZ = ZoneInfo(IST)


@dataclass(slots=True)
class GuardResult:
    allowed: bool
    guard: str = ""
    detail: str = ""
    halt: bool = False        # trip the kill switch
    flatten: bool = False     # close open positions as well

    @classmethod
    def ok(cls) -> GuardResult:
        return cls(allowed=True)

    def __bool__(self) -> bool:
        return self.allowed


@dataclass(slots=True)
class GuardContext:
    """Everything the guards need. Assembled once per decision."""

    now: dt.datetime
    plan: TradePlan | None = None
    signal: Signal | None = None
    intent: OrderIntent | None = None

    positions: list[Position] = field(default_factory=list)
    open_position_count: int = 0
    realised_pnl: float = 0.0
    unrealised_pnl: float = 0.0
    available_funds: float | None = None
    last_tick_age: float | None = None      # seconds since the most recent tick
    reconciled: bool = True
    reconcile_detail: str = ""
    trades_today: int = 0
    reference_price: float | None = None    # for the slippage check
    contract_expiry: dt.date | None = None  # of the instrument being traded

    @property
    def total_pnl(self) -> float:
        return self.realised_pnl + self.unrealised_pnl


def _exposure(intent: OrderIntent, price: float) -> float:
    """Rupees actually at risk in a contract, not quantity times quote.

    MCX gold is quoted per 10 g while quantity is in grams, so 100 of GOLDM at
    153,327 is Rs 15.3 lakh of gold rather than Rs 15.3 crore. Taking the
    quote literally made a Rs 2 lakh notional cap reject every gold order it
    ever saw a price for.
    """
    return price * intent.quantity * intent.instrument.price_multiplier


GuardFn = Callable[[GuardContext], GuardResult]


class GuardChain:
    """Runs the guards in order and stops at the first block."""

    def __init__(
        self,
        risk: GlobalRisk,
        killswitch: KillSwitch,
        calendar: MarketCalendar,
        journal: Journal | None = None,
    ) -> None:
        self.risk = risk
        self.killswitch = killswitch
        self.calendar = calendar
        self.journal = journal

    # === signal-stage guards =============================================
    def check_signal(self, ctx: GuardContext) -> GuardResult:
        """Run before an order is built. Cheapest checks first."""
        for guard in (
            self._kill_switch,          # 1
            self._market_open,          # 7
            self._feed_fresh,           # 6
            self._signal_fresh,         # 14
            self._not_duplicate,        # 5
            self._square_off_window,    # 12
            self._contract_alive,       # 15
            self._daily_loss,           # 2
            self._reconciled,           # 11
            self._trades_today,         # 3
            self._open_positions,       # 4
        ):
            result = guard(ctx)
            if not result.allowed:
                self._record(result, ctx)
                return result
        return GuardResult.ok()

    # === order-stage guards ==============================================
    def check_order(self, ctx: GuardContext) -> GuardResult:
        """Run after the resolver, when quantity and instrument are known."""
        for guard in (
            self._kill_switch,          # re-checked: a human may have halted meanwhile
            self._fat_finger,           # 10
            self._sufficient_funds,     # 8
            self._slippage,             # 13
        ):
            result = guard(ctx)
            if not result.allowed:
                self._record(result, ctx)
                return result
        return GuardResult.ok()

    def _record(self, result: GuardResult, ctx: GuardContext) -> None:
        log.warning("guard.blocked", guard=result.guard, detail=result.detail,
                    plan=ctx.plan.name if ctx.plan else None, halt=result.halt)
        if self.journal is not None:
            self.journal.record_guard(
                result.guard, False, result.detail,
                plan=ctx.plan.name if ctx.plan else None,
                signal_key=ctx.signal.dedupe_key if ctx.signal else None,
            )

    # === individual guards ===============================================
    def _kill_switch(self, ctx: GuardContext) -> GuardResult:
        """1. Checked on every decision, never cached — the point is that a
        human can stop the bot without it cooperating."""
        state = self.killswitch.state()
        if state.halted:
            return GuardResult(False, "kill_switch",
                               f"halted via {state.source}: {state.reason}")
        return GuardResult.ok()

    def _market_open(self, ctx: GuardContext) -> GuardResult:
        """7. Advisory only — the holiday list can be stale, which is why the
        feed-staleness guard is the real backstop."""
        if not self.calendar.is_open(ctx.now):
            return GuardResult(False, "market_hours",
                               f"market {self.calendar.describe(ctx.now)}")
        return GuardResult.ok()

    def _feed_fresh(self, ctx: GuardContext) -> GuardResult:
        """6. Trading on a frozen feed is the classic silent disaster: the
        socket is up, prices simply stopped, and the strategy keeps acting on a
        price that stopped being true minutes ago."""
        age = ctx.last_tick_age
        if age is None:
            return GuardResult(False, "feed_staleness",
                               "no tick received yet — refusing to trade on an "
                               "unproven feed")
        if age > self.risk.feed_staleness_seconds:
            return GuardResult(False, "feed_staleness",
                               f"last tick {age:.0f}s ago, limit "
                               f"{self.risk.feed_staleness_seconds}s")
        return GuardResult.ok()

    def _signal_fresh(self, ctx: GuardContext) -> GuardResult:
        """14. Catches restart replays and late webhook retries — a signal from
        a bar that closed twenty minutes ago is history, not an instruction."""
        if ctx.signal is None:
            return GuardResult.ok()
        if ctx.signal.source == "adopted":
            # An adopted signal is deliberately historical: it is the trade the
            # strategy is already in, from a bar that closed hours or days ago.
            # Staleness is the reason it is being adopted, not a fault. Every
            # other guard still applies -- notably the duplicate check, which
            # is what stops a restart re-entering the same trade.
            return GuardResult.ok()
        bar_ts = ctx.signal.bar_ts
        if bar_ts.tzinfo is None:
            bar_ts = bar_ts.replace(tzinfo=IST_TZ)
        # Localise `now` the same way. Mixing one aware and one naive datetime
        # raises TypeError, and a guard that raises does not block — it takes
        # the whole signal path down with it, silently, inside a task.
        now = ctx.now if ctx.now.tzinfo else ctx.now.replace(tzinfo=IST_TZ)
        # Age from when the bar CLOSED, not when it opened. A bar carries its
        # open time, so measuring from that makes every signal look exactly one
        # timeframe stale the instant it fires — harmless at 5 minutes, but it
        # blocked every single signal on a 2-hour plan, where a fresh bar is
        # 7200s old by this arithmetic against a 120s limit.
        span = 0.0
        if ctx.plan is not None and ctx.plan.signal.tf.is_intraday:
            span = (ctx.plan.signal.tf.minutes or 0) * 60
        raw_age = (now - bar_ts).total_seconds()
        age = raw_age - span
        if age > self.risk.signal_staleness_seconds:
            return GuardResult(False, "signal_staleness",
                               f"bar {bar_ts:%H:%M} closed {age:.0f}s ago, limit "
                               f"{self.risk.signal_staleness_seconds}s")
        # Deliberately the *raw* age: a bar whose OPEN is in the future means a
        # clock or timezone fault. Its close being in the future is ordinary --
        # that is just a bar still forming.
        if raw_age < -60:
            return GuardResult(False, "signal_staleness",
                               f"bar {bar_ts:%H:%M} is in the future by "
                               f"{-raw_age:.0f}s — clock or timezone problem")
        return GuardResult.ok()

    def _not_duplicate(self, ctx: GuardContext) -> GuardResult:
        """5. Backed by a UNIQUE constraint in the journal, so it survives a
        restart. An in-memory set would let a crash-and-restart re-fire a signal
        the bot had already acted on."""
        if ctx.signal is None or self.journal is None:
            return GuardResult.ok()
        if self.journal.has_signal(ctx.signal.dedupe_key):
            return GuardResult(False, "duplicate_signal",
                               f"already acted on {ctx.signal.dedupe_key}")
        return GuardResult.ok()

    def _square_off_window(self, ctx: GuardContext) -> GuardResult:
        """12. No new entries near the close — an intraday position opened at
        15:09 gets force-closed at 15:10 for nothing but costs."""
        if ctx.plan is None:
            return GuardResult.ok()
        cutoff = ctx.plan.risk.square_off_time
        if cutoff is None:          # plan holds across sessions; no cutoff
            return GuardResult.ok()
        if ctx.now.time() >= cutoff:
            return GuardResult(False, "square_off_window",
                               f"past the {cutoff:%H:%M} square-off cutoff")
        return GuardResult.ok()

    def _contract_alive(self, ctx: GuardContext) -> GuardResult:
        """15. Refuse to open a position in a contract that is expiring.

        A plan can pin one contract by security id, which never rolls. Past
        expiry that id is dead: no ticks, no fills, and the plan simply goes
        quiet. The days before expiry are their own hazard — liquidity drains
        and the exchange raises margins — so entries stop before the last day,
        while exits stay allowed so an open position can always be closed.
        """
        if ctx.plan is None or ctx.signal is None:
            return GuardResult.ok()
        if ctx.signal.action != "BUY":
            return GuardResult.ok()          # never block a close
        expiry = getattr(ctx, "contract_expiry", None)
        if expiry is None:
            return GuardResult.ok()
        days = (expiry - ctx.now.date()).days
        if days < 0:
            return GuardResult(False, "contract_expired",
                               f"contract expired on {expiry:%d %b %Y} — roll the "
                               "plan to the next one")
        if days <= self.risk.min_days_to_expiry:
            return GuardResult(False, "contract_expiring",
                               f"contract expires {expiry:%d %b %Y}, {days} day(s) "
                               f"away — no new entries inside "
                               f"{self.risk.min_days_to_expiry} days")
        return GuardResult.ok()

    def _daily_loss(self, ctx: GuardContext) -> GuardResult:
        """2. Halts *and* flattens, and persists so a restart stays halted."""
        limit = self.risk.max_daily_loss
        if limit <= 0:
            return GuardResult.ok()
        if ctx.total_pnl <= -abs(limit):
            return GuardResult(
                False, "daily_loss_cap",
                f"P&L Rs.{ctx.total_pnl:,.0f} breached the Rs.{-abs(limit):,.0f} "
                f"limit (realised {ctx.realised_pnl:,.0f}, "
                f"unrealised {ctx.unrealised_pnl:,.0f})",
                halt=True, flatten=self.risk.flatten_on_halt,
            )
        return GuardResult.ok()

    def _reconciled(self, ctx: GuardContext) -> GuardResult:
        """11. If our book disagrees with the broker's, our view of reality is
        wrong and every downstream decision is built on it."""
        if not ctx.reconciled:
            return GuardResult(False, "position_reconciliation",
                               ctx.reconcile_detail or
                               "local position book disagrees with the broker",
                               halt=True)
        return GuardResult.ok()

    def _trades_today(self, ctx: GuardContext) -> GuardResult:
        """3. Per-plan churn limit."""
        if ctx.plan is None:
            return GuardResult.ok()
        cap = ctx.plan.risk.max_trades_per_day
        if cap > 0 and ctx.trades_today >= cap:
            return GuardResult(False, "max_trades_per_day",
                               f"{ctx.trades_today} trades today, cap {cap}")
        return GuardResult.ok()

    def _open_positions(self, ctx: GuardContext) -> GuardResult:
        """4. Account-wide concurrency cap across all plans."""
        cap = self.risk.max_open_positions
        if cap > 0 and ctx.open_position_count >= cap:
            return GuardResult(False, "max_open_positions",
                               f"{ctx.open_position_count} open, cap {cap}")
        return GuardResult.ok()

    def _fat_finger(self, ctx: GuardContext) -> GuardResult:
        """10. Bounds on a single order. Catches a lot-size misread or a config
        typo before the exchange sees it."""
        intent = ctx.intent
        if intent is None:
            return GuardResult.ok()
        if intent.quantity <= 0:
            return GuardResult(False, "fat_finger",
                               f"non-positive quantity {intent.quantity}")
        if intent.quantity > self.risk.max_qty_per_order:
            return GuardResult(False, "fat_finger",
                               f"quantity {intent.quantity} exceeds cap "
                               f"{self.risk.max_qty_per_order}")
        reference = ctx.reference_price or intent.price
        if reference <= 0:
            # No usable price. Blocking an *entry* here is the point: sizing a
            # new position against an unknown price is how a lot-size misread
            # reaches the exchange. An exit is the opposite — it reduces risk,
            # and must never be held up for want of a quote.
            if intent.signal is not None and intent.signal.action != "BUY":
                return GuardResult.ok()
            return GuardResult(False, "fat_finger",
                               "no reference price to size this order against")
        notional = _exposure(intent, reference)
        if notional > self.risk.max_notional_per_order:
            return GuardResult(False, "fat_finger",
                               f"exposure Rs.{notional:,.0f} exceeds cap "
                               f"Rs.{self.risk.max_notional_per_order:,.0f}")
        return GuardResult.ok()

    def _sufficient_funds(self, ctx: GuardContext) -> GuardResult:
        """8. Fails closed: if the balance could not be read, block. Sending an
        order against an unknown balance risks a rejection that leaves the
        strategy believing it holds a position it does not."""
        intent = ctx.intent
        if intent is None:
            return GuardResult.ok()
        # Exits reduce exposure and must never be blocked for want of margin.
        if intent.signal is not None and intent.signal.action != "BUY":
            return GuardResult.ok()
        if ctx.available_funds is None:
            return GuardResult(False, "insufficient_funds",
                               "could not read available balance — refusing to "
                               "send an order against an unknown balance")
        reference = ctx.reference_price or intent.price
        if reference <= 0:
            return GuardResult(False, "insufficient_funds",
                               "no reference price — cannot tell whether this "
                               "order is affordable")
        # A future blocks margin; a long option costs its whole premium.
        exposure = _exposure(intent, reference)
        kind = intent.instrument.instrument_type
        long_option = (kind in OPTION_TYPES
                       and intent.transaction_type is TransactionType.BUY)
        needed = (exposure if (long_option or kind is InstrumentType.EQUITY)
                  else exposure * self.risk.assumed_margin_pct)
        if needed > ctx.available_funds:
            return GuardResult(False, "insufficient_funds",
                               f"need about Rs.{needed:,.0f}, available "
                               f"Rs.{ctx.available_funds:,.0f}")
        return GuardResult.ok()

    def _slippage(self, ctx: GuardContext) -> GuardResult:
        """13. Rejects a limit order priced far from the live market, which
        usually means a stale reference rather than a real opportunity."""
        intent = ctx.intent
        if intent is None or intent.price <= 0 or ctx.reference_price is None:
            return GuardResult.ok()
        if ctx.reference_price <= 0:
            return GuardResult.ok()
        drift = abs(intent.price - ctx.reference_price) / ctx.reference_price
        if drift > 0.05:
            return GuardResult(False, "slippage",
                               f"order price {intent.price:,.2f} is "
                               f"{drift:.1%} away from the market "
                               f"{ctx.reference_price:,.2f}")
        return GuardResult.ok()


__all__ = ["GuardChain", "GuardContext", "GuardResult"]
