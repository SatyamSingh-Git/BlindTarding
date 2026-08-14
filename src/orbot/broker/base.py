"""Broker-neutral interfaces.

Everything above this line — strategy, guards, resolver, position book, journal,
engine — talks to these protocols and never to a specific broker. Swapping
brokers is then a config line rather than a rewrite, and if one broker turns out
flaky during expiry-day volatility you can move without re-validating the whole
system.

Two things are deliberately *not* abstracted:

**`ExchangeSegment` stays the shared vocabulary.** It already describes Indian
market structure accurately (index, cash, F&O, currency, commodity, per
exchange), so each adapter maps it to whatever codes its broker uses —
`NSE_FNO` becomes `"NFO"` for Angel and `"NSE_FNO"` for Dhan. Inventing a second
neutral enum on top would add a layer that only renames things.

**Security ids are opaque strings.** Dhan calls them securityId, Angel calls
them token, and they are *different numbers for the same contract*. Nothing
above the broker layer ever constructs one — it only passes back what the
instrument registry produced. That is what stops a Dhan id being sent to Angel,
which would resolve to a real but entirely unrelated instrument.
"""

from __future__ import annotations

import datetime as dt
from typing import Callable, Protocol, runtime_checkable

from ..models import Candle, Instrument, Order, OrderIntent, Position, Tick


@runtime_checkable
class InstrumentSource(Protocol):
    """Resolves human intent to a tradeable contract."""

    def get(self, segment, security_id: str) -> Instrument: ...
    def index(self, symbol: str) -> Instrument: ...
    def equity(self, symbol: str, *, exchange=None) -> Instrument: ...
    def expiries(self, underlying: str, instrument_type, *,
                 on_or_after: dt.date | None = None) -> list[dt.date]: ...
    def select_expiry(self, underlying: str, instrument_type, rule: str, *,
                      today: dt.date | None = None) -> dt.date: ...
    def strikes(self, underlying: str, instrument_type, expiry: dt.date,
                option_type) -> list[float]: ...
    def option(self, underlying: str, option_type, strike: float,
               expiry: dt.date, *, instrument_type=None) -> Instrument: ...
    def select_option(self, underlying: str, option_type, spot: float,
                      expiry: dt.date, *, instrument_type=None,
                      offset: int = 0) -> Instrument: ...
    def future(self, underlying: str, expiry: dt.date, *,
               instrument_type=None) -> Instrument: ...


@runtime_checkable
class MarketData(Protocol):
    """Historical bars. Live ticks come through `LiveFeed`."""

    def intraday(self, instrument: Instrument, interval: int,
                 start: dt.datetime, end: dt.datetime, *,
                 oi: bool = False) -> list[Candle]: ...

    def daily(self, instrument: Instrument, start: dt.date, end: dt.date, *,
              oi: bool = False) -> list[Candle]: ...

    @property
    def native_intervals(self) -> tuple[int, ...]:
        """Intraday intervals in minutes this broker aggregates itself.

        Matters for fidelity, not convenience: a native interval's bar
        boundaries come from the exchange and can be checked against a chart
        directly. Anything else we aggregate, with boundaries we chose.
        """
        ...


@runtime_checkable
class OrderGateway(Protocol):
    """Order placement and account state."""

    def place_order(self, intent: OrderIntent) -> Order: ...
    def modify_order(self, order_id: str, **fields) -> dict: ...
    def cancel_order(self, order_id: str) -> dict: ...
    def get_order(self, order_id: str) -> Order: ...
    def find_order_by_correlation(self, correlation_id: str) -> Order | None: ...
    def orders(self) -> list[Order]: ...
    def positions(self) -> list[Position]: ...
    def available_balance(self) -> float: ...

    @property
    def has_static_ip_proxy(self) -> bool: ...


@runtime_checkable
class LiveFeed(Protocol):
    """Streaming ticks, with reconnection handled internally."""

    def add(self, instruments) -> None: ...
    async def run(self) -> None: ...
    async def stop(self) -> None: ...
    def seconds_since_last_tick(self) -> float | None: ...


class Broker(Protocol):
    """One broker's full surface. Adapters implement this."""

    name: str

    def instruments(self, *, underlyings: list[str] | None = None) -> InstrumentSource: ...
    def market_data(self) -> MarketData: ...
    def orders_gateway(self) -> OrderGateway: ...
    def live_feed(self, on_tick: Callable[[Tick], None]) -> LiveFeed: ...
    def token_status(self) -> str: ...


__all__ = ["Broker", "InstrumentSource", "MarketData", "OrderGateway", "LiveFeed"]
