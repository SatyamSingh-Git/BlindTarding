"""Order management.

Two implementations behind one interface, so paper and live run the *same* code
path everywhere else. If paper mode took a different route through the resolver
or the guards, two weeks of clean paper trading would prove nothing about the
live path.

`PaperOMS` simulates fills; `LiveOMS` talks to Dhan. Everything upstream —
strategy, guards, resolver, position book, journal — is identical.

The hard part of live order management is not placing orders, it is knowing what
happened. Three rules:

**Never blind-retry a placement.** If the request times out, the order may or
may not have reached the exchange. Retrying can double a position. The client
resolves the ambiguity by querying `correlationId` instead, and if that fails,
`OrderStateUnknown` propagates and the bot halts rather than guessing.

**Poll to a terminal state.** An order that is PENDING is not a position.
Updating the book on placement rather than on fill is how bots come to believe
they hold something they do not.

**A partial fill is a real position.** PART_TRADED with 32 of 65 lots is
thirty-two lots of exposure, and the exit must size from the position, not the
order.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from abc import ABC, abstractmethod
from zoneinfo import ZoneInfo

import structlog

from ..broker.dhan_client import DhanAPIError, DhanClient, OrderStateUnknown
from pathlib import Path
import json
from dataclasses import replace

from ..constants import (IST, ExchangeSegment, OPTION_TYPES, InstrumentType, OrderStatus,
                         OrderType, TransactionType)
from ..models import Order, OrderIntent, Position

log = structlog.get_logger(__name__)
IST_TZ = ZoneInfo(IST)


class OMS(ABC):
    mode: str = "abstract"

    @abstractmethod
    def place(self, intent: OrderIntent, *, reference_price: float | None = None) -> Order:
        ...

    @abstractmethod
    def poll(self, order: Order) -> Order:
        ...

    @abstractmethod
    def cancel(self, order: Order) -> None:
        ...

    @abstractmethod
    def positions(self) -> list[Position]:
        ...

    @abstractmethod
    def available_funds(self) -> float | None:
        ...

    def settle(self, order: Order, *, timeout: float = 20.0,
               interval: float = 0.5) -> Order:
        """Poll until terminal or the timeout expires.

        A timeout is not treated as a failure — the order is live at the
        exchange and its state is simply not yet known. Cancelling on timeout
        would be worse: it can race a fill and leave the book wrong in the other
        direction. The caller sees a non-terminal status and the reconciliation
        guard picks up any drift.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if order.status.is_terminal:
                return order
            time.sleep(interval)
            try:
                order = self.poll(order)
            except DhanAPIError as exc:
                log.warning("oms.poll_failed", order_id=order.order_id,
                            error=str(exc)[:200])
                break
        if not order.status.is_terminal:
            log.warning("oms.settle_timeout", order_id=order.order_id,
                        status=order.status.value, filled=order.filled_qty,
                        hint="order is live at the exchange; reconciliation will "
                             "detect any drift")
        return order


class PaperOMS(OMS):
    """Simulated fills. No network calls, no money.

    Fills at the reference price plus a configurable slippage, immediately and
    in full. That is deliberately optimistic and should be read as such: real
    fills on an illiquid strike will be worse. It exists to exercise the whole
    pipeline end to end, not to predict P&L.
    """

    mode = "paper"

    def __init__(self, *, starting_funds: float = 1_000_000.0,
                 slippage_pct: float = 0.05,
                 slippage_ticks: float | None = 1.0,
                 margin_pct: float = 0.11,
                 state_path: Path | None = None) -> None:
        self.funds = starting_funds
        self.starting_funds = starting_funds
        self.slippage_pct = slippage_pct
        self.slippage_ticks = slippage_ticks
        # Futures and options are margin products: buying one blocks a fraction
        # of the contract value, it does not cost the contract value. Charging
        # the full notional drove paper funds to minus Rs 1.5 crore on a
        # Rs 3 lakh account the first time a gold future was bought.
        self.margin_pct = margin_pct
        self._orders: dict[str, Order] = {}
        self._positions: dict[tuple[str, str], Position] = {}
        self._marks: dict[tuple[str, str], float] = {}
        self._kinds: dict[tuple[str, str], InstrumentType] = {}
        self._multipliers: dict[tuple[str, str], float] = {}
        # A live broker remembers your positions across a restart; an in-memory
        # simulator does not, and the duplicate-signal guard then correctly
        # refuses to re-enter -- leaving the bot flat and blind to a trade it
        # believes it is in. Persisting them keeps paper honest about restarts.
        self._state_path = state_path
        self._restore()

    def place(self, intent: OrderIntent, *, reference_price: float | None = None) -> Order:
        price = reference_price or intent.price
        if price <= 0:
            # A market order with no reference is unfillable in simulation, and
            # inventing a price would make paper results meaningless.
            raise ValueError(
                f"paper fill needs a reference price for {intent.instrument.display_name}"
            )

        # Slippage in ticks where configured, because that is how it behaves:
        # you cross the spread and lose a tick or two. As a percentage it scales
        # with price, so 0.05% cost GOLDM 77 ticks and SILVER100 one -- the same
        # setting, two orders of magnitude apart, for no reason but the size of
        # the number the contract happens to be quoted in.
        tick = intent.instrument.tick_size or 0.05
        if self.slippage_ticks is not None:
            offset = self.slippage_ticks * tick
        else:
            offset = price * (self.slippage_pct / 100.0)
        fill = (price + offset if intent.transaction_type is TransactionType.BUY
                else price - offset)
        fill = round(fill / tick) * tick

        order = Order(
            order_id=f"PAPER-{uuid.uuid4().hex[:10]}",
            status=OrderStatus.TRADED,
            security_id=intent.instrument.security_id,
            segment=intent.instrument.segment,
            transaction_type=intent.transaction_type,
            quantity=intent.quantity,
            filled_qty=intent.quantity,
            avg_price=fill,
            price=intent.price,
            correlation_id=intent.correlation_id,
            raw={"simulated": True, "reference_price": price},
        )
        self._orders[order.order_id] = order
        self._apply(intent, order)
        self._marks[intent.instrument.key] = fill
        self._kinds[intent.instrument.key] = intent.instrument.instrument_type
        self._multipliers[intent.instrument.key] = intent.instrument.price_multiplier
        self._recompute_funds()
        self._persist()
        log.info("oms.paper_fill", instrument=intent.instrument.display_name,
                 side=intent.transaction_type.value, qty=intent.quantity,
                 price=round(fill, 2), funds=round(self.funds, 2),
                 blocked=round(self._blocked_margin(), 2))
        return order

    def _blocked_margin(self) -> float:
        """Capital tied up by open positions.

        Three different things, and conflating them is how paper funds went to
        minus Rs 1.5 crore on a Rs 3 lakh account:

        * a **future** blocks margin, a fraction of contract value;
        * a **long option** costs its full premium — there is nothing to
          margin, you have already paid;
        * a **short option** blocks margin, like a future.
        """
        total = 0.0
        for key, position in self._positions.items():
            if position.net_qty == 0:
                continue
            kind = self._kinds.get(key)
            price = self._marks.get(key) or position.avg_price
            notional = (abs(position.net_qty) * price
                        * self._multipliers.get(key, 1.0))
            if kind in OPTION_TYPES and position.net_qty > 0:
                total += notional                     # premium paid in full
            elif kind is InstrumentType.EQUITY:
                total += notional                     # cash segment
            else:
                total += notional * self.margin_pct   # futures, short options
        return total

    def _recompute_funds(self) -> None:
        """Cash = what you started with, plus what you have realised, minus what
        is currently tied up as margin. Derived rather than accumulated, so a
        partial exit cannot leave a stale blocked amount behind."""
        realised = sum(p.realised for p in self._positions.values())
        self.funds = self.starting_funds + realised - self._blocked_margin()

    def _apply(self, intent: OrderIntent, order: Order) -> None:
        key = intent.instrument.key
        position = self._positions.get(key)
        if position is None:
            position = Position(security_id=intent.instrument.security_id,
                                segment=intent.instrument.segment, net_qty=0)
            self._positions[key] = position
        signed = (order.filled_qty if intent.transaction_type is TransactionType.BUY
                  else -order.filled_qty)
        new_qty = position.net_qty + signed
        if position.net_qty != 0 and (position.net_qty > 0) != (signed > 0):
            closed = min(abs(signed), abs(position.net_qty))
            direction = 1 if position.net_qty > 0 else -1
            position.realised += (direction * closed
                                  * (order.avg_price - position.avg_price)
                                  * intent.instrument.price_multiplier)
        if new_qty != 0 and (position.net_qty == 0 or (position.net_qty > 0) == (signed > 0)):
            total = position.avg_price * abs(position.net_qty) + order.avg_price * abs(signed)
            position.avg_price = total / abs(new_qty)
        elif new_qty == 0:
            position.avg_price = 0.0
        position.net_qty = new_qty
        position.product_type = intent.product_type

    def poll(self, order: Order) -> Order:
        return self._orders.get(order.order_id, order)

    def cancel(self, order: Order) -> None:
        stored = self._orders.get(order.order_id)
        if stored and not stored.status.is_terminal:
            stored.status = OrderStatus.CANCELLED

    def positions(self) -> list[Position]:
        # Copies, deliberately. A live client parses fresh objects out of JSON;
        # handing out our own would let a caller's book alias our state, so the
        # OMS mutating a position on fill would silently apply that fill to the
        # book as well -- and the book's own apply_fill would then apply it a
        # second time, turning a closed long into a phantom short.
        return [replace(p) for p in self._positions.values() if p.net_qty != 0]

    def available_funds(self) -> float | None:
        return self.funds

    # --- persistence -----------------------------------------------------
    def _restore(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("oms.paper_state_unreadable", error=str(exc))
            return
        if not isinstance(raw, dict):
            return
        for row in raw.get("positions", []):
            try:
                key = (row["segment"], row["security_id"])
                self._positions[key] = Position(
                    security_id=row["security_id"],
                    segment=ExchangeSegment(row["segment"]),
                    net_qty=int(row["net_qty"]),
                    avg_price=float(row["avg_price"]),
                    realised=float(row.get("realised", 0.0)))
                self._marks[key] = float(row.get("mark", row["avg_price"]))
                self._kinds[key] = InstrumentType(row["kind"])
                self._multipliers[key] = float(row.get("multiplier", 1.0))
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("oms.paper_state_row_skipped", error=str(exc))
        self._recompute_funds()
        if self._positions:
            log.info("oms.paper_state_restored", positions=len(self._positions),
                     funds=round(self.funds, 2))

    def _persist(self) -> None:
        if self._state_path is None:
            return
        payload = {"positions": [
            {"security_id": p.security_id, "segment": p.segment.value,
             "net_qty": p.net_qty, "avg_price": p.avg_price,
             "realised": p.realised, "mark": self._marks.get(k, p.avg_price),
             "kind": (self._kinds.get(k) or InstrumentType.EQUITY).value,
             "multiplier": self._multipliers.get(k, 1.0)}
            for k, p in self._positions.items() if p.net_qty != 0]}
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:
            log.warning("oms.paper_state_unwritable", error=str(exc))


class LiveOMS(OMS):
    """Real orders against Dhan, routed through the static-IP proxy."""

    mode = "live"

    def __init__(self, client: DhanClient) -> None:
        self.client = client
        if not client.has_static_ip_proxy:
            log.warning(
                "oms.no_static_ip_proxy",
                hint="Dhan requires a whitelisted static IP for order placement. "
                     "Without DHAN_ORDER_PROXY this only works if this machine's "
                     "own IP is already whitelisted.",
            )

    def place(self, intent: OrderIntent, *, reference_price: float | None = None) -> Order:
        log.info("oms.placing", instrument=intent.instrument.display_name,
                 side=intent.transaction_type.value, qty=intent.quantity,
                 order_type=intent.order_type.value,
                 correlation_id=intent.correlation_id)
        try:
            order = self.client.place_order(intent)
        except OrderStateUnknown:
            # Deliberately not caught here. The caller must halt — this is the
            # one failure where continuing risks doubling a position.
            raise
        log.info("oms.placed", order_id=order.order_id, status=order.status.value)
        return order

    def poll(self, order: Order) -> Order:
        fresh = self.client.get_order(order.order_id)
        fresh.correlation_id = fresh.correlation_id or order.correlation_id
        return fresh

    def cancel(self, order: Order) -> None:
        try:
            self.client.cancel_order(order.order_id)
            log.info("oms.cancelled", order_id=order.order_id)
        except DhanAPIError as exc:
            log.warning("oms.cancel_failed", order_id=order.order_id,
                        error=str(exc)[:200])

    def positions(self) -> list[Position]:
        return self.client.positions()

    def available_funds(self) -> float | None:
        try:
            return self.client.available_balance()
        except DhanAPIError as exc:
            # Returning None rather than a number: the funds guard fails closed
            # on None, which is the safe reading of "we do not know".
            log.warning("oms.funds_unavailable", error=str(exc)[:200])
            return None


def build_oms(mode: str, client: DhanClient | None, *,
              starting_funds: float = 1_000_000.0,
              slippage_pct: float = 0.05,
              slippage_ticks: float | None = 1.0,
              state_path: Path | None = None) -> OMS:
    if mode == "live":
        if client is None:
            raise ValueError("live mode requires a Dhan client")
        return LiveOMS(client)
    return PaperOMS(starting_funds=starting_funds, slippage_pct=slippage_pct,
                    slippage_ticks=slippage_ticks, state_path=state_path)


__all__ = ["OMS", "PaperOMS", "LiveOMS", "build_oms"]
