"""Local position book, reconciled against the broker.

We keep our own view of what is held so decisions do not need a network round
trip, but the broker is always the authority. When the two disagree, the correct
response is to stop trading and alert — not to pick whichever looks more
plausible. A disagreement means our model of reality is wrong, and every
decision built on it is suspect.

Common causes worth recognising in the logs: a manual trade in the Dhan app, a
partial fill we stopped polling too early, an order rejected after we recorded
it as sent, or an auto square-off by the broker near the close.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from ..constants import ExchangeSegment, TransactionType
from ..models import Order, OrderIntent, Position

log = structlog.get_logger(__name__)

PositionKey = tuple[str, str]


@dataclass(slots=True)
class Mismatch:
    key: PositionKey
    local_qty: int
    broker_qty: int
    display_name: str = ""

    def __str__(self) -> str:
        name = self.display_name or f"{self.key[0]}/{self.key[1]}"
        return f"{name}: local {self.local_qty:+d} vs broker {self.broker_qty:+d}"


@dataclass(slots=True)
class ReconcileResult:
    matched: bool
    mismatches: list[Mismatch] = field(default_factory=list)
    broker_positions: list[Position] = field(default_factory=list)

    @property
    def detail(self) -> str:
        if self.matched:
            return "local book agrees with the broker"
        return "; ".join(str(m) for m in self.mismatches)


class PositionBook:
    def __init__(self) -> None:
        self._positions: dict[PositionKey, Position] = {}
        self._multipliers: dict[PositionKey, float] = {}
        self._names: dict[PositionKey, str] = {}
        # How much of each order has already been folded in, keyed by the
        # order's identity. Held here rather than on the Order object: the book
        # owns what it has applied, and an Order is a plain snapshot that may be
        # replaced wholesale by the next poll.
        self._applied: dict[str, int] = {}

    # --- local updates ---------------------------------------------------
    def apply_fill(self, intent: OrderIntent, order: Order) -> Position:
        """Fold a fill into the book.

        Safe to call repeatedly with the same cumulative `filled_qty` — only the
        delta since the last call is applied, so a poll loop reporting an
        unchanged state cannot double-count, and a partial fill that later
        completes adds only the remainder.
        """
        self._multipliers[intent.instrument.key] = (
            intent.instrument.price_multiplier)
        key = intent.instrument.key
        self._names[key] = intent.instrument.display_name
        position = self._positions.get(key)
        if position is None:
            position = Position(security_id=intent.instrument.security_id,
                                segment=intent.instrument.segment, net_qty=0)
            self._positions[key] = position

        signed = (order.filled_qty if intent.transaction_type is TransactionType.BUY
                  else -order.filled_qty)
        # Keyed on the *intent*, not the order. The intent is the authoritative
        # identity of an order attempt: it carries a correlation id from the
        # moment it is built, whereas an order has no broker id until placement
        # succeeds, and two freshly built orders can collide on a blank one.
        ident = (intent.correlation_id or order.correlation_id
                 or order.order_id or f"{key}:{id(intent)}")
        delta = signed - self._applied.get(ident, 0)
        if delta == 0:
            return position
        self._applied[ident] = signed

        new_qty = position.net_qty + delta
        if position.net_qty == 0 or (position.net_qty > 0) == (delta > 0):
            # Opening or adding: weighted-average the entry price.
            total_cost = position.avg_price * abs(position.net_qty) + \
                order.avg_price * abs(delta)
            position.avg_price = total_cost / abs(new_qty) if new_qty else 0.0
        elif new_qty == 0 or (new_qty > 0) != (position.net_qty > 0):
            # Closing out, possibly flipping.
            closed = min(abs(delta), abs(position.net_qty))
            direction = 1 if position.net_qty > 0 else -1
            position.realised += (direction * closed
                                  * (order.avg_price - position.avg_price)
                                  * intent.instrument.price_multiplier)
            if new_qty == 0:
                position.avg_price = 0.0
            elif (new_qty > 0) != (position.net_qty > 0):
                position.avg_price = order.avg_price

        position.net_qty = new_qty
        position.product_type = intent.product_type
        log.info("position.updated", instrument=intent.instrument.display_name,
                 net_qty=position.net_qty, avg_price=round(position.avg_price, 2),
                 realised=round(position.realised, 2))
        return position

    def set_multiplier(self, key: PositionKey, value: float) -> None:
        self._multipliers[key] = value

    def mark(self, key: PositionKey, last_price: float) -> None:
        position = self._positions.get(key)
        if position is None or position.net_qty == 0:
            return
        # Through the contract multiplier, or MCX gold reads ten times too big:
        # GOLDM is 100 g quoted per 10 g, so a point is worth Rs 10 on a
        # 100-quantity lot, not Rs 100.
        position.unrealised = (position.net_qty * (last_price - position.avg_price)
                               * self._multipliers.get(key, 1.0))

    # --- access ----------------------------------------------------------
    def get(self, key: PositionKey) -> Position | None:
        return self._positions.get(key)

    def for_instrument(self, segment: ExchangeSegment, security_id: str) -> Position | None:
        return self._positions.get((segment.value, str(security_id)))

    def open_positions(self) -> list[Position]:
        return [p for p in self._positions.values() if p.net_qty != 0]

    def name_for(self, key: PositionKey) -> str:
        return self._names.get(key, f"{key[0]}/{key[1]}")

    @property
    def realised(self) -> float:
        return sum(p.realised for p in self._positions.values())

    @property
    def unrealised(self) -> float:
        return sum(p.unrealised for p in self.open_positions())

    def __len__(self) -> int:
        return len(self.open_positions())

    # --- reconciliation --------------------------------------------------
    def reconcile(self, broker_positions: list[Position]) -> ReconcileResult:
        """Compare against the broker. Only *open* positions are compared —
        a flat instrument the broker has forgotten about is not a discrepancy."""
        broker_map = {p.key: p for p in broker_positions if p.net_qty != 0}
        local_map = {p.key: p for p in self.open_positions()}

        mismatches: list[Mismatch] = []
        for key in set(broker_map) | set(local_map):
            local_qty = local_map[key].net_qty if key in local_map else 0
            broker_qty = broker_map[key].net_qty if key in broker_map else 0
            if local_qty != broker_qty:
                mismatches.append(Mismatch(key, local_qty, broker_qty,
                                           self.name_for(key)))

        if mismatches:
            log.error("position.mismatch",
                      detail="; ".join(str(m) for m in mismatches))
        return ReconcileResult(not mismatches, mismatches, broker_positions)

    def adopt(self, broker_positions: list[Position]) -> None:
        """Replace the local book with the broker's view.

        Only ever called deliberately — at startup, or by a human resolving a
        mismatch. Never automatically on a discrepancy, because silently
        adopting the broker's numbers would hide the bug that caused the drift.
        """
        self._positions = {p.key: p for p in broker_positions if p.net_qty != 0}
        log.warning("position.adopted", count=len(self._positions))


__all__ = ["PositionBook", "ReconcileResult", "Mismatch"]
