"""Core domain objects.

Everything crossing a module boundary is one of these. Broker JSON is parsed into
them at the edge so no dict-shaped payloads leak into the engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .constants import (
    ExchangeSegment,
    InstrumentType,
    OptionType,
    OrderStatus,
    OrderType,
    ProductType,
    TransactionType,
    Validity,
)


# How much metal a contract holds, and the unit its price is quoted in.
# Both in grams, because MCX quotes gold per 10 g and silver per kilogram while
# expressing quantity in yet another unit.
#
# Hardcoding one multiplier per symbol was wrong: GOLD and GOLDM are quoted
# identically but have different lot sizes, so the same 0.1 that is right for
# GOLDM made GOLD worth 10 paise a point instead of Rs 100 -- out by a factor
# of a thousand, silently, on a contract worth Rs 1.55 crore.
_CONTRACT_SPECS = {
    # symbol:        (grams in the contract, grams the quote refers to)
    "GOLD":          (1_000, 10),      # 1 kg,   quoted per 10 g
    "GOLDM":         (100, 10),        # 100 g,  quoted per 10 g
    "GOLDGUINEA":    (8, 10),          # 8 g
    "GOLDPETAL":     (1, 10),          # 1 g
    "SILVER":        (30_000, 1_000),  # 30 kg,  quoted per kg
    "SILVERM":       (5_000, 1_000),   # 5 kg
    "SILVERMIC":     (1_000, 1_000),   # 1 kg
    "SILVER100":     (1_000, 10),      # 1 kg,   quoted per 10 g
}


@dataclass(frozen=True, slots=True)
class Instrument:
    """One tradeable (or observable) line from the scrip master."""

    security_id: str
    segment: ExchangeSegment
    instrument_type: InstrumentType
    symbol: str                       # SYMBOL_NAME, e.g. "NIFTY"
    display_name: str                 # e.g. "NIFTY 29 SEP 29150 CALL"
    lot_size: int = 1
    tick_size: float = 0.05
    expiry: date | None = None
    strike: float | None = None
    option_type: OptionType | None = None
    underlying_symbol: str | None = None
    series: str | None = None
    freeze_qty: int | None = None

    @property
    def price_multiplier(self) -> float:
        """Rupees of P&L per 1 unit of quantity per 1 point of price.

        Usually 1: buy 200 of something quoted at 2,378 and you are exposed to
        200 x 2,378. MCX commodities break that, because the contract size, the
        quote unit and the quantity unit are three different things.

        Derived rather than tabulated, so it stays right when a contract's lot
        size differs: one whole contract is worth (grams / quote_grams) rupees
        per point, spread over `lot_size` units of quantity.

            GOLDM   100 g quoted per 10 g, lot 100  ->  10 / 100  = 0.1
            GOLD    1 kg quoted per 10 g,  lot 1    ->  100 / 1   = 100
        """
        spec = _CONTRACT_SPECS.get(self.symbol.upper())
        if spec is None or not self.lot_size:
            return 1.0
        grams, quote_grams = spec
        return (grams / quote_grams) / self.lot_size

    @property
    def key(self) -> tuple[str, str]:
        """Identity used everywhere as a dict key."""
        return (self.segment.value, self.security_id)

    @property
    def is_option(self) -> bool:
        return self.option_type is not None

    def __str__(self) -> str:
        return f"{self.display_name} [{self.segment.value}/{self.security_id}]"


@dataclass(frozen=True, slots=True)
class Tick:
    """A single feed update. `ts` is exchange time, tz-aware IST."""

    security_id: str
    segment: ExchangeSegment
    ltp: float
    ts: datetime
    ltq: int | None = None
    volume: int | None = None
    atp: float | None = None
    oi: int | None = None
    day_open: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    day_close: float | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.segment.value, self.security_id)


@dataclass(slots=True)
class Candle:
    """OHLCV bar. `ts` is the bar's OPEN time (TradingView convention)."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    oi: int | None = None
    closed: bool = False

    def update(self, price: float, qty: int = 0) -> None:
        if price > self.high:
            self.high = price
        if price < self.low:
            self.low = price
        self.close = price
        self.volume += qty

    @classmethod
    def opening(cls, ts: datetime, price: float, qty: int = 0) -> Candle:
        return cls(ts=ts, open=price, high=price, low=price, close=price, volume=qty)


class Action(str):
    """Signal direction. Plain str subclass so it serialises cleanly."""

    BUY = "BUY"
    SELL = "SELL"
    EXIT = "EXIT"


@dataclass(frozen=True, slots=True)
class Signal:
    """What the strategy emits. Deliberately says nothing about *what* to trade —
    the resolver turns this into an execution instrument."""

    plan: str
    action: str                       # Action.BUY | SELL | EXIT
    bar_ts: datetime                  # bar that produced it — the dedupe key
    price: float                      # signal-instrument price at emission
    source: str = "engine"            # engine | webhook | manual
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        """Idempotency key. Guard 5 rejects a repeat of this."""
        return f"{self.plan}|{self.bar_ts.isoformat()}|{self.action}"


@dataclass(slots=True)
class OrderIntent:
    """A fully-resolved order, before it is sent anywhere."""

    plan: str
    instrument: Instrument
    transaction_type: TransactionType
    quantity: int
    product_type: ProductType
    order_type: OrderType
    price: float = 0.0
    trigger_price: float = 0.0
    validity: Validity = Validity.DAY
    correlation_id: str = ""
    signal: Signal | None = None

    def to_payload(self, client_id: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "dhanClientId": client_id,
            "transactionType": self.transaction_type.value,
            "exchangeSegment": self.instrument.segment.value,
            "productType": self.product_type.value,
            "orderType": self.order_type.value,
            "validity": self.validity.value,
            "securityId": self.instrument.security_id,
            "quantity": int(self.quantity),
            "price": round(float(self.price), 2),
        }
        if self.correlation_id:
            body["correlationId"] = self.correlation_id[:30]
        if self.order_type in (OrderType.STOP_LOSS, OrderType.STOP_LOSS_MARKET):
            body["triggerPrice"] = round(float(self.trigger_price), 2)
        return body


@dataclass(slots=True)
class Order:
    """Broker-side order state."""

    order_id: str
    status: OrderStatus
    security_id: str = ""
    segment: ExchangeSegment | None = None
    transaction_type: TransactionType | None = None
    quantity: int = 0
    filled_qty: int = 0
    avg_price: float = 0.0
    price: float = 0.0
    correlation_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def pending_qty(self) -> int:
        return max(0, self.quantity - self.filled_qty)


@dataclass(slots=True)
class Position:
    security_id: str
    segment: ExchangeSegment
    net_qty: int
    avg_price: float = 0.0
    realised: float = 0.0
    unrealised: float = 0.0
    product_type: ProductType | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_flat(self) -> bool:
        return self.net_qty == 0

    @property
    def key(self) -> tuple[str, str]:
        return (self.segment.value, self.security_id)
