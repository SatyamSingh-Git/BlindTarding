"""The trading loop.

Wires feed -> candles -> strategy -> guards -> resolver -> OMS, and runs the
periodic housekeeping that unattended operation depends on: reconciliation,
mark-to-market, square-off and heartbeats.

Two decisions shape the whole file.

**Paper and live share every line except the OMS.** The strategy, guards,
resolver, position book and journal do not know which mode they are in. If paper
took a different path, a clean fortnight of paper trading would prove nothing
about the live path.

**SELL means exit, and the exit sizes from the position.** Config says how much
to open; only the broker knows how much is actually held. Exiting the configured
size when a partial fill left a different quantity would leave a residual
position or reverse into a short.

Blocking broker calls are pushed to a thread so a slow REST round trip cannot
stall the feed reader and starve the candle builder.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field
from typing import Callable
from zoneinfo import ZoneInfo

import structlog

from ..broker.dhan_client import OrderStateUnknown
from ..broker.instruments import InstrumentRegistry
from ..config import GlobalRisk, Settings, TradePlan
from ..constants import IST, ExchangeSegment
from ..execution.guards import GuardChain, GuardContext, GuardResult
from ..execution.oms import OMS
from ..execution.positions import PositionBook
from ..execution.resolver import ExecutionResolver, ResolutionError
from ..models import Action, Candle, Instrument, Position, Signal, Tick
from ..ops.journal import DuplicateSignal, Journal
from ..ops.killswitch import KillSwitch
from ..ops.market_calendar import MarketCalendar
from ..ops.notifier import Notifier
from .candles import CandleSeries, session_start_for
from .history import warmup_candles
from .strategy import SvmkrUtHmaOrb, params_from_config
from .timeframe import Timeframe

log = structlog.get_logger(__name__)
IST_TZ = ZoneInfo(IST)


@dataclass(slots=True)
class PlanState:
    """One configured plan, plus everything it needs at runtime."""

    plan: TradePlan
    instrument: Instrument
    timeframe: Timeframe
    series: CandleSeries
    strategy: SvmkrUtHmaOrb
    resolver: ExecutionResolver
    position_key: tuple[str, str] | None = None   # what this plan currently holds
    # Latched once a bracket exit is scheduled, so the ticks that keep arriving
    # while the exit order is in flight cannot queue a second flatten.
    bracket_fired: bool = False
    # Where the stop and target are measured from. Normally the fill price;
    # when a mid-trade signal is adopted it is the signal's price, so the
    # bracket keeps the geometry the strategy was tested with.
    bracket_ref_price: float | None = None
    trades_today: int = 0
    last_signal_bar: dt.datetime | None = None
    signals_seen: int = 0
    orders_sent: int = 0

    @property
    def name(self) -> str:
        return self.plan.name

    @property
    def is_flat(self) -> bool:
        return self.position_key is None


class TradingEngine:
    def __init__(
        self,
        *,
        settings: Settings,
        plans: list[TradePlan],
        risk: GlobalRisk,
        registry: InstrumentRegistry,
        client: object | None,
        oms: OMS,
        journal: Journal,
        notifier: Notifier,
        killswitch: KillSwitch,
        calendar: MarketCalendar,
        make_feed: Callable[[Callable[[Tick], None]], object] | None = None,
        feed_instrument: type | None = None,
    ) -> None:
        self.settings = settings
        # Supplied by the broker bundle. The engine never names a broker.
        self.make_feed = make_feed
        self.feed_instrument = feed_instrument
        self.risk = risk
        self.registry = registry
        self.client = client
        self.oms = oms
        self.journal = journal
        self.notifier = notifier
        self.killswitch = killswitch
        self.calendar = calendar

        self.guards = GuardChain(risk, killswitch, calendar, journal)
        self.book = PositionBook()
        self.states: dict[str, PlanState] = {}
        self.feed = None
        self.broker_name = ""
        self.supports_correlation_id = True
        self.last_prices: dict[tuple[str, str], float] = {}
        # Instruments that appeared in the last broker snapshot, so the next one
        # can explicitly record any that have since gone flat.
        self._snapshotted: set[tuple[str, str]] = set()
        self._running = False
        self._tasks: list[asyncio.Task] = []

        for plan in plans:
            if plan.enabled:
                self.states[plan.name] = self._build_state(plan)

    # --- setup -----------------------------------------------------------
    def _build_state(self, plan: TradePlan) -> PlanState:
        instrument = self.registry.get(plan.signal.exchange_segment,
                                       plan.signal.security_id)
        timeframe = plan.signal.tf
        if not timeframe.is_intraday:
            raise ValueError(
                f"plan {plan.name!r} uses {timeframe.label}; live trading needs an "
                "intraday timeframe (daily bars close once a day)"
            )
        params = params_from_config(plan.strategy.params)
        return PlanState(
            plan=plan,
            instrument=instrument,
            timeframe=timeframe,
            series=CandleSeries(timeframe.minutes,
                                session_start=session_start_for(instrument.segment)),
            strategy=SvmkrUtHmaOrb(params, plan=plan.name),
            resolver=ExecutionResolver(self.registry, plan),
        )

    def validate(self) -> list[str]:
        """Static checks across every enabled plan, run before the market opens."""
        problems: list[str] = []
        if not self.states:
            problems.append("no enabled plans — nothing to run")
        today = dt.datetime.now(IST_TZ).date()
        for state in self.states.values():
            if (state.plan.strategy.adopt_fill == "signal"
                    and self.oms.mode == "live"):
                problems.append(
                    f"plan {state.name!r} sets adopt_fill: signal, which fills an "
                    "adopted trade at a price from the past. That is a paper-only "
                    "fiction for keeping a run comparable to the backtest — no "
                    "exchange will fill you there. Use 'market' for live."
                )
            problems.extend(state.resolver.validate())
            expiry = state.instrument.expiry
            if expiry is not None and expiry < today:
                problems.append(
                    f"plan {state.name!r} trades {state.instrument.display_name}, "
                    f"which expired on {expiry:%d %b %Y}. Re-resolve it with "
                    "`orbot resolve` — a dead security id returns no data and "
                    "places no orders, so the plan would simply go quiet."
                )
            elif expiry is not None and (expiry - today).days <= 3:
                log.warning("engine.expiry_near", plan=state.name,
                            instrument=state.instrument.display_name,
                            expiry=expiry.isoformat(),
                            days=(expiry - today).days)
            if (not state.timeframe.is_native
                    and not state.plan.strategy.resampling_validated):
                problems.append(
                    f"plan {state.name!r} uses {state.timeframe.label}, which the "
                    "broker does not serve natively and which is therefore built "
                    "by resampling. Check its bars against the chart, then set "
                    "strategy.resampling_validated: true to accept them"
                )
        return problems

    async def warmup(self) -> None:
        """Seed every plan's candle series from history.

        Starting cold would mean signalling off a half-warmed ATR. The strategy
        refuses to emit until it has `min_bars`, so a failed warmup shows up as
        silence rather than as bad trades — but silence is still a failure.
        """
        if self.client is None:
            log.warning("engine.no_client", detail="skipping warmup; no data client")
            return
        for n, state in enumerate(self.states.values()):
            if n:
                # Space the calls out. Warming several plans in the same
                # instant is exactly what trips Angel's rate limiter.
                await asyncio.sleep(1.5)
            need = state.strategy.params.min_bars
            candles = await asyncio.to_thread(
                warmup_candles, self.client, state.instrument, state.timeframe, need
            )
            seeded = state.series.seed(candles)
            level = log.info if seeded >= need else log.error
            level("engine.warmed", plan=state.name, bars=seeded, needed=need,
                  instrument=state.instrument.display_name)
            if seeded < need:
                self.notifier.error(
                    "warmup incomplete",
                    f"{state.name}: {seeded}/{need} bars — will not signal until warm",
                )

    async def adopt_broker_positions(self) -> None:
        """Start from the broker's view rather than assuming flat.

        A restart mid-session with an open position must not begin by believing
        it is flat, or the next BUY would double the exposure.
        """
        positions = await asyncio.to_thread(self.oms.positions)
        if positions:
            # An adopted position never passes through apply_fill, so the book
            # has no way to know a GOLDM point is worth Rs 10 rather than
            # Rs 100 unless it is told here.
            def multiplier_for(key: tuple[str, str]) -> float:
                try:
                    return self.registry.get(ExchangeSegment(key[0]),
                                             key[1]).price_multiplier
                except Exception:
                    return 1.0
            self.book.adopt(positions, multiplier_for=multiplier_for)
            self.notifier.send(
                "adopted existing positions at startup:\n"
                + "\n".join(f"{p.security_id} {p.net_qty:+d} @ {p.avg_price:,.2f}"
                            for p in positions)
            )
            # Attribute each position to a plan where the instrument matches, so
            # a SELL knows what to close.
            for state in self.states.values():
                for position in positions:
                    if position.segment == state.instrument.segment and \
                            position.security_id == state.instrument.security_id:
                        state.position_key = position.key

    # --- feed ------------------------------------------------------------
    async def _await_first_tick(self, timeout: float = 20.0) -> bool:
        """Wait for the feed to prove itself before trading on it.

        Returns False on timeout rather than raising: a quiet feed is a reason
        to skip adoption, not to abort startup, and the guards will keep
        refusing entries until ticks arrive.
        """
        if self.feed is None:
            return False
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self.feed.seconds_since_last_tick() is not None:
                return True
            await asyncio.sleep(0.25)
        log.warning("engine.no_first_tick", seconds=timeout,
                    detail="feed delivered nothing; skipping adoption")
        return False

    async def adopt_open_signals(self) -> None:
        """Join a trade the strategy is already in, for plans that opt in.

        Starting flat mid-trade means the chart shows a live long while the bot
        holds nothing, and it stays that way until the *next* full cycle. This
        closes that gap — but it is opt-in, because the fill happens at today's
        price rather than the signal's, so the resulting trade is not the one
        the backtest measured.

        Refused when price has already run past where the stop would sit: by
        the strategy's own rules that trade is dead, and adopting it would open
        a position that its own bracket should close immediately.
        """
        # Symmetry first: if the strategy has exited while we were not
        # running, close what we are still holding. Missing an exit is worse
        # than missing an entry -- an entry not taken costs an opportunity, a
        # position held past its exit keeps running risk the strategy has
        # already decided against, with no stop between it and the next signal.
        for state in self.states.values():
            if state.is_flat:
                continue
            bars = list(state.series.bars)
            if state.strategy.open_position_entry(bars) is not None:
                continue          # strategy is still long; holding is correct
            log.warning("engine.stale_position", plan=state.name,
                        detail="strategy is flat but a position is open — the "
                               "exit fired while the engine was not running")
            self.notifier.send(
                f"{state.name}: the strategy exited while the bot was stopped, "
                f"but the position is still open. Closing it now.")
            await self.close_plan(state, "exit missed while stopped")

        for state in self.states.values():
            if not (state.plan.strategy.adopt_open_signal and state.is_flat):
                continue
            bars = list(state.series.bars)
            entry = state.strategy.open_position_entry(bars)
            if entry is None:
                continue
            signal, signal_price = entry
            price = self.last_prices.get(state.instrument.key) or bars[-1].close
            at_signal = (state.plan.strategy.adopt_fill == "signal"
                         and self.oms.mode == "paper")
            drift = price - signal_price
            sl = state.plan.risk.sl_points
            if sl is not None and drift <= -abs(sl):
                log.warning("engine.adopt_skipped", plan=state.name,
                            signal_price=signal_price, price=price,
                            detail="price is already past the stop for this signal")
                self.notifier.send(
                    f"{state.name}: not adopting the open {signal.action} from "
                    f"{signal.bar_ts:%d %b %H:%M} @ {signal_price:,.2f} — "
                    f"price is {price:,.2f}, already past its {sl:,.0f}-point stop")
                continue
            log.warning("engine.adopting", plan=state.name, at_signal=at_signal,
                        bar=signal.bar_ts.isoformat(),
                        signal_price=signal_price, fill_price=price, drift=drift)
            if at_signal:
                self.notifier.send(
                    f"{state.name}: adopting the open {signal.action} signalled "
                    f"{signal.bar_ts:%d %b %H:%M} @ {signal_price:,.2f}\n"
                    f"filling at the signal price, not the market's "
                    f"{price:,.2f} ({drift:+,.2f} away) — paper only, so this "
                    f"run stays comparable to the backtest")
            else:
                self.notifier.send(
                    f"{state.name}: adopting the open {signal.action} signalled "
                    f"{signal.bar_ts:%d %b %H:%M} @ {signal_price:,.2f}\n"
                    f"entering at {price:,.2f} ({drift:+,.2f} vs the signal) — "
                    f"this is not the entry the backtest measured")
            if sl is not None and drift > 0:
                log.warning("engine.adopt_risk", plan=state.name,
                            planned_stop_points=sl,
                            actual_risk_points=sl + drift,
                            detail="stop stays at the signal's level, so the "
                                   "risk from this fill is larger than planned")
            ctx = await self._context(state, signal)
            # Adoption is a real entry and must clear the same gate every other
            # entry does. Calling _enter() directly skipped the kill switch,
            # market hours, feed freshness, the duplicate check, the daily loss
            # cap and the position limits -- nine guards, bypassed at startup,
            # which is the least supervised moment the bot has.
            verdict = self.guards.check_signal(ctx)
            if not verdict.allowed:
                log.warning("engine.adopt_blocked", plan=state.name,
                            guard=verdict.guard, detail=verdict.detail)
                self.notifier.guard_blocked(verdict.guard, verdict.detail,
                                            plan=state.name)
                await self._maybe_halt(verdict)
                continue
            try:
                self.journal.record_signal(signal)
            except DuplicateSignal:
                log.info("engine.adopt_duplicate", plan=state.name,
                         key=signal.dedupe_key,
                         detail="already acted on this signal in an earlier run")
                continue
            await self._enter(state, signal, ctx,
                              fill_price=signal_price if at_signal else None)
            # Measure the bracket from the signal, not from today's fill, so the
            # trade keeps the risk geometry it was tested with.
            state.bracket_ref_price = signal_price

    def _subscriptions(self) -> list[object]:
        make = self.feed_instrument
        if make is None:
            return []
        wanted: dict[tuple[str, str], object] = {}
        for state in self.states.values():
            key = state.instrument.key
            wanted[key] = make(state.instrument.segment,
                               state.instrument.security_id)
        # Held instruments too, so open positions can be marked to market.
        for position in self.book.open_positions():
            wanted[position.key] = make(position.segment, position.security_id)
        return list(wanted.values())

    def on_tick(self, tick: Tick) -> None:
        self.last_prices[tick.key] = tick.ltp
        self.book.mark(tick.key, tick.ltp)
        for state in self.states.values():
            # A bracket is checked against the *executed* contract, which is
            # rarely the signal instrument — a NIFTY plan signals on the index
            # and holds an option.
            if state.position_key == tick.key:
                self._check_bracket(state, tick.ltp)
            if state.instrument.key != tick.key:
                continue
            closed = state.series.on_tick(tick)
            if closed is not None:
                self._queue_bar(state, closed)

    def _check_bracket(self, state: PlanState, price: float) -> None:
        """Fire the fixed stop / target between bar closes.

        Checked on every tick rather than at bar close: a 2-hour bar is a long
        time to sit past your stop, and the backtest that chose these levels
        assumed the stop triggers when price touches it, not hours later.
        """
        risk = state.plan.risk
        if state.bracket_fired or state.position_key is None:
            return
        if risk.sl_points is None and risk.target_points is None:
            return
        position = self.book.get(state.position_key)
        if position is None or position.net_qty == 0 or position.avg_price <= 0:
            return
        long = position.net_qty > 0
        entry = state.bracket_ref_price or position.avg_price
        hit: str | None = None
        if risk.sl_points is not None:
            stop = entry - risk.sl_points if long else entry + risk.sl_points
            if (price <= stop) if long else (price >= stop):
                hit = f"stop {stop:,.2f}"
        if hit is None and risk.target_points is not None:
            target = entry + risk.target_points if long else entry - risk.target_points
            if (price >= target) if long else (price <= target):
                hit = f"target {target:,.2f}"
        if hit is None:
            return
        # Latch before scheduling: ticks keep arriving while the exit order is
        # in flight, and a second flatten would try to sell what is already sold.
        state.bracket_fired = True
        log.warning("engine.bracket_hit", plan=state.name, entry=entry,
                    price=price, hit=hit)
        self._tasks.append(asyncio.create_task(
            self.flatten(f"{state.name} {hit} hit at {price:,.2f}")))

    def _queue_bar(self, state: PlanState, candle: Candle) -> None:
        # Bar handling touches the broker, so it runs as a task rather than
        # inline in the feed reader.
        self._tasks.append(asyncio.create_task(self._on_bar_close(state, candle)))

    async def _on_bar_close(self, state: PlanState, candle: Candle) -> None:
        try:
            signal = state.strategy.latest_signal(list(state.series.bars))
        except Exception as exc:
            log.exception("engine.strategy_failed", plan=state.name)
            self.notifier.error("strategy failed", f"{state.name}: {exc}")
            return
        if signal is None:
            return
        if state.last_signal_bar == signal.bar_ts:
            return
        state.last_signal_bar = signal.bar_ts
        state.signals_seen += 1
        log.info("engine.signal", plan=state.name, action=signal.action,
                 bar=signal.bar_ts.isoformat(), price=signal.price)
        self.notifier.signal(signal)
        await self.handle_signal(state, signal)

    # --- signal handling --------------------------------------------------
    async def handle_signal(self, state: PlanState, signal: Signal) -> None:
        ctx = await self._context(state, signal)

        verdict = self.guards.check_signal(ctx)
        if not verdict.allowed:
            self.notifier.guard_blocked(verdict.guard, verdict.detail, plan=state.name)
            await self._maybe_halt(verdict)
            return

        try:
            self.journal.record_signal(signal)
        except DuplicateSignal:
            log.info("engine.duplicate_signal", key=signal.dedupe_key)
            return

        if signal.action == Action.BUY:
            await self._enter(state, signal, ctx)
        else:
            await self._exit(state, signal, ctx)

    async def _enter(self, state: PlanState, signal: Signal, ctx: GuardContext,
                     *, fill_price: float | None = None) -> None:
        if not state.is_flat:
            log.info("engine.already_long", plan=state.name,
                     detail="BUY while already holding; ignoring")
            return
        spot = self.last_prices.get(state.instrument.key, signal.price)
        try:
            intent = state.resolver.resolve_entry(signal, spot=spot)
        except (ResolutionError, Exception) as exc:
            log.error("engine.resolve_failed", plan=state.name, error=str(exc))
            self.notifier.error("could not resolve contract", f"{state.name}: {exc}")
            return
        await self._send(state, intent, ctx, fill_price=fill_price)

    async def _exit(self, state: PlanState, signal: Signal, ctx: GuardContext) -> None:
        if state.is_flat:
            log.info("engine.already_flat", plan=state.name,
                     detail="SELL with no position; nothing to close")
            return
        position = self.book.get(state.position_key)  # type: ignore[arg-type]
        if position is None or position.net_qty == 0:
            state.position_key = None
            state.bracket_fired = False
            state.bracket_ref_price = None
            return
        try:
            intent = state.resolver.resolve_exit(position, signal=signal)
        except ResolutionError as exc:
            log.error("engine.exit_resolve_failed", plan=state.name, error=str(exc))
            self.notifier.error("could not resolve exit", f"{state.name}: {exc}")
            return
        await self._send(state, intent, ctx, is_exit=True)

    async def _send(self, state: PlanState, intent, ctx: GuardContext,
                    *, is_exit: bool = False,
                    fill_price: float | None = None) -> None:
        reference = fill_price or self.last_prices.get(intent.instrument.key)
        if (reference is None and intent.signal is not None
                and intent.instrument.key == state.instrument.key):
            # Fall back to the signal's price *before* the guards run, not
            # after: at startup no tick has arrived, and the notional and funds
            # checks need a price to mean anything.
            #
            # Only when the traded contract IS the signal contract. An option
            # plan signals on the index and buys a strike, so the signal's
            # price is NIFTY at 24,287 while the option trades near 100 —
            # borrowing it would overstate the exposure fifteenfold.
            reference = intent.signal.price
        if reference is None and self.client is not None:
            # Nothing in the feed for this contract. An option strike is
            # resolved at signal time and never subscribed, so this is the
            # normal case there — ask the broker rather than send an order
            # against a price we do not have.
            try:
                reference = await asyncio.to_thread(self.client.ltp,
                                                    intent.instrument)
            except Exception as exc:
                log.warning("engine.ltp_failed", plan=state.name,
                            instrument=intent.instrument.display_name,
                            error=str(exc))
        ctx.intent = intent
        ctx.reference_price = reference

        verdict = self.guards.check_order(ctx)
        if not verdict.allowed:
            self.notifier.guard_blocked(verdict.guard, verdict.detail, plan=state.name)
            await self._maybe_halt(verdict)
            return

        try:
            order = await asyncio.to_thread(self.oms.place, intent,
                                            reference_price=reference)
        except OrderStateUnknown as exc:
            # The one failure we never retry or guess past.
            self.killswitch.trip(f"order state unknown: {exc}")
            self.notifier.halted(f"ORDER STATE UNKNOWN — resolve manually before "
                                 f"restarting.\n{exc}")
            return
        except Exception as exc:
            log.exception("engine.place_failed", plan=state.name)
            self.notifier.error("order failed", f"{state.name}: {exc}")
            return

        state.orders_sent += 1
        signal_row = None
        self.journal.record_order(intent, order, mode=self.oms.mode,
                                  signal_id=signal_row)
        self.notifier.order_placed(intent, order, mode=self.oms.mode)

        order = await asyncio.to_thread(self.oms.settle, order)
        self.journal.update_order(order)

        if order.filled_qty > 0:
            self.book.apply_fill(intent, order)
            self.notifier.order_filled(order, name=intent.instrument.display_name)
            state.trades_today += 1
            position = self.book.get(intent.instrument.key)
            state.bracket_fired = False
            state.bracket_ref_price = None
            state.position_key = (intent.instrument.key
                                  if position and position.net_qty != 0 else None)
            if is_exit and state.position_key is None:
                log.info("engine.flat", plan=state.name)
        elif order.status.is_terminal:
            log.warning("engine.no_fill", plan=state.name,
                        status=order.status.value, order_id=order.order_id)
            self.notifier.error("order not filled",
                                f"{state.name}: {order.status.value}")

    # --- context and halting ---------------------------------------------
    async def _context(self, state: PlanState | None,
                       signal: Signal | None) -> GuardContext:
        funds = await asyncio.to_thread(self.oms.available_funds)
        age = self.feed.seconds_since_last_tick() if self.feed else None
        realised, unrealised = self.book.realised, self.book.unrealised
        return GuardContext(
            now=dt.datetime.now(IST_TZ),
            plan=state.plan if state else None,
            signal=signal,
            positions=self.book.open_positions(),
            open_position_count=len(self.book),
            realised_pnl=realised,
            unrealised_pnl=unrealised,
            available_funds=funds,
            last_tick_age=age,
            trades_today=state.trades_today if state else 0,
            contract_expiry=state.instrument.expiry if state else None,
        )

    def _realised_today(self) -> float:
        """Realised P&L for today, including anything booked before a restart.

        The position book is rebuilt from nothing each run, so reading realised
        P&L from it reported Rs 0 on a day that had already banked Rs 1,710 --
        every restart silently erased the day's result.
        """
        carried = self.oms.realised_on(dt.datetime.now(IST_TZ).date())
        return carried if carried else self.book.realised

    async def _maybe_halt(self, verdict: GuardResult) -> None:
        if not verdict.halt:
            return
        self.killswitch.trip(f"{verdict.guard}: {verdict.detail}")
        self.journal.update_daily_pnl(self._realised_today(), self.book.unrealised,
                                      halted=True)
        self.notifier.halted(f"{verdict.guard}\n{verdict.detail}")
        if verdict.flatten:
            await self.flatten("guard halt")

    async def close_plan(self, state: PlanState, reason: str) -> None:
        """Close one plan's position, leaving every other plan alone.

        flatten() is the emergency lever: a halt or a breached loss cap means
        stop everything. Reaching for it to close a single stale position
        closed the other plan too, whose own strategy was still long and had
        given no exit at all.
        """
        if state.position_key is None:
            return
        position = self.book.get(state.position_key)
        if position is None or position.net_qty == 0:
            state.position_key = None
            return
        log.warning("engine.close_plan", plan=state.name, reason=reason,
                    qty=position.net_qty)
        try:
            intent = state.resolver.resolve_exit(position)
            reference = self.last_prices.get(position.key)
            order = await asyncio.to_thread(self.oms.place, intent,
                                            reference_price=reference)
            order = await asyncio.to_thread(self.oms.settle, order)
            self.journal.record_order(intent, order, mode=self.oms.mode)
            if order.filled_qty > 0:
                self.book.apply_fill(intent, order)
                state.position_key = None
                state.bracket_fired = False
                state.bracket_ref_price = None
        except Exception as exc:
            log.exception("engine.close_plan_failed", plan=state.name)
            self.notifier.error("could not close position",
                                f"{state.name}: {exc}")

    async def flatten(self, reason: str) -> None:
        """Close every open position. Used by halts and by square-off."""
        positions = self.book.open_positions()
        if not positions:
            return
        log.warning("engine.flattening", reason=reason, count=len(positions))
        self.notifier.send(f"FLATTENING {len(positions)} position(s) — {reason}")
        for position in positions:
            state = next((s for s in self.states.values()
                          if s.position_key == position.key), None)
            resolver = state.resolver if state else None
            if resolver is None:
                log.error("engine.flatten_no_plan", position=position.key,
                          detail="no plan owns this position; close it manually")
                self.notifier.error("cannot flatten",
                                    f"{position.security_id} {position.net_qty:+d} "
                                    "has no owning plan — close it manually")
                continue
            try:
                intent = resolver.resolve_exit(position)
                reference = self.last_prices.get(position.key)
                order = await asyncio.to_thread(self.oms.place, intent,
                                                reference_price=reference)
                order = await asyncio.to_thread(self.oms.settle, order)
                self.journal.record_order(intent, order, mode=self.oms.mode)
                if order.filled_qty > 0:
                    self.book.apply_fill(intent, order)
                    if state:
                        state.position_key = None
                        state.bracket_fired = False
                        state.bracket_ref_price = None
            except Exception as exc:
                log.exception("engine.flatten_failed", position=position.key)
                self.notifier.error("flatten failed",
                                    f"{position.security_id}: {exc}")

    # --- periodic loops ---------------------------------------------------
    async def _reconcile_loop(self) -> None:
        interval = max(10, self.risk.reconcile_interval_seconds)
        while self._running:
            await asyncio.sleep(interval)
            try:
                broker = await asyncio.to_thread(self.oms.positions)
            except Exception as exc:
                log.warning("engine.reconcile_failed", error=str(exc)[:200])
                continue
            result = self.book.reconcile(broker)
            # The broker is authoritative for quantity and average price, but it
            # has not seen a tick -- only the local book marks to market. Taking
            # the broker's snapshot verbatim showed every position at zero
            # unrealised next to a header quoting a non-zero P&L, which is two
            # answers to the same question on one screen.
            for position in broker:
                local = self.book.get(position.key)
                if local is not None and position.net_qty == local.net_qty:
                    position.unrealised = local.unrealised
            # Also record instruments that have gone flat. The broker only
            # reports what it holds, so a closed position simply stops
            # appearing -- and a panel that takes the latest row per instrument
            # then shows it open forever. Gold sat on the dashboard for hours
            # after the halt had already sold it.
            live = {p.key for p in broker}
            closed = [Position(security_id=k[1], segment=ExchangeSegment(k[0]),
                               net_qty=0, avg_price=0.0)
                      for k in self._snapshotted - live]
            self._snapshotted = live
            self.journal.snapshot_positions(list(broker) + closed, source="broker")
            if not result.matched:
                self.killswitch.trip(f"position mismatch: {result.detail}")
                self.notifier.halted(f"POSITION MISMATCH\n{result.detail}\n\n"
                                     "Local book disagrees with the broker. "
                                     "Resolve manually before resuming.")
            self.journal.update_daily_pnl(self._realised_today(), self.book.unrealised,
                                          halted=self.killswitch.is_halted)

    async def _square_off_loop(self) -> None:
        while self._running:
            await asyncio.sleep(20)
            now = dt.datetime.now(IST_TZ)
            for state in self.states.values():
                if state.is_flat:
                    continue
                cutoff = state.plan.risk.square_off_time
                if cutoff is None:      # plan holds across sessions
                    continue
                if now.time() >= cutoff:
                    log.warning("engine.square_off", plan=state.name)
                    await self.flatten(f"{state.name} square-off {cutoff:%H:%M}")

    async def _bar_clock(self) -> None:
        """Close bars on the clock so a signal does not wait for the next trade."""
        while self._running:
            await asyncio.sleep(1)
            now = dt.datetime.now(IST_TZ)
            for state in self.states.values():
                closed = state.series.flush(now)
                if closed is not None:
                    self._queue_bar(state, closed)
            self._tasks = [t for t in self._tasks if not t.done()]

    async def _heartbeat_loop(self, minutes: int = 30) -> None:
        while self._running:
            await asyncio.sleep(minutes * 60)
            if not self.calendar.is_open():
                continue
            age = self.feed.seconds_since_last_tick() if self.feed else None
            self.notifier.heartbeat(
                f"mode {self.oms.mode} | positions {len(self.book)} | "
                f"P&L Rs.{self.book.realised + self.book.unrealised:,.0f} | "
                f"last tick {age:.0f}s ago" if age is not None else "no ticks yet"
            )

    # --- lifecycle --------------------------------------------------------
    async def run(self) -> None:
        problems = self.validate()
        if problems:
            for problem in problems:
                log.error("engine.invalid_config", detail=problem)
            self.notifier.error("configuration rejected", "\n".join(problems))
            raise SystemExit(1)

        self.journal.start_run(self.oms.mode)
        self._running = True
        mode_note = ("PAPER — simulated fills, no real orders" if self.oms.mode == "paper"
                     else "LIVE — real money")
        # Describe the exchange the plans actually trade. Reporting NSE here
        # put "354 min to close" in the startup notice while the banner two
        # lines above it correctly said 834.
        segments = {st.instrument.segment for st in self.states.values()}
        segment = segments.pop() if len(segments) == 1 else None
        self.notifier.send(f"ORBot starting\nmode: {mode_note}\n"
                           f"plans: {', '.join(self.states)}\n"
                           f"market: {self.calendar.describe(segment=segment)}")

        # What the last run left on screen, so the first reconciliation can
        # explicitly clear anything that has closed since.
        try:
            self._snapshotted = self.journal.last_snapshot_keys()
        except Exception as exc:
            log.warning("engine.snapshot_seed_failed", error=str(exc)[:120])

        await self.warmup()
        await self.adopt_broker_positions()

        if self.make_feed is not None:
            self.feed = self.make_feed(self.on_tick)
            self.feed.add(self._subscriptions())
            self._tasks.append(asyncio.create_task(self.feed.run()))

        # Adoption has to come after the feed, not before it. It is an entry
        # like any other and so must clear the feed-freshness guard, which by
        # design refuses to trade until a tick has actually arrived — run
        # first, it blocked itself every single time.
        await self._await_first_tick()
        await self.adopt_open_signals()

        for loop in (self._bar_clock, self._reconcile_loop,
                     self._square_off_loop, self._heartbeat_loop):
            self._tasks.append(asyncio.create_task(loop()))

        log.info("engine.running", mode=self.oms.mode, plans=list(self.states))
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        log.info("engine.stopping")
        if self.feed is not None:
            await self.feed.stop()
        for task in self._tasks:
            task.cancel()
        self.journal.update_daily_pnl(self._realised_today(), self.book.unrealised,
                                      halted=self.killswitch.is_halted)
        self.journal.end_run()
        self.notifier.send(
            f"ORBot stopped\npositions {len(self.book)} | "
            f"realised Rs.{self.book.realised:,.0f}"
        )


__all__ = ["TradingEngine", "PlanState"]
