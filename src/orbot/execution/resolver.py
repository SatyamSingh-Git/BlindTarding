"""Signal instrument -> execution instrument.

The strategy says "BUY on NIFTY at 24,287". This turns that into "buy 65 of
NIFTY 20 AUG 24300 CE, security id 45102". Everything asset-class-specific lives
here, which is what lets one engine cover index options, futures and cash equity
without the strategy knowing any of it.

The failure mode this module is built to prevent: returning a *valid but wrong*
contract. A wrong strike, a stale expiry, or an index security id sent as a
tradeable one all produce orders that look entirely normal in the logs. So every
lookup either resolves exactly or raises — nothing here falls back to a nearby
instrument.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import uuid

import structlog

from ..broker.instruments import InstrumentError, InstrumentRegistry
from ..config import ExecutionConfig, LegConfig, TradePlan
from ..constants import (
    FUTURE_TYPES,
    OPTION_TYPES,
    ExchangeSegment,
    InstrumentType,
    OptionType,
    OrderType,
    TransactionType,
)
from ..models import Action, Instrument, OrderIntent, Position, Signal

log = structlog.get_logger(__name__)

# "ATM", "ITM1", "OTM2", "ATM+1", "ATM-2", or an absolute strike like "24500"
_OFFSET_PATTERN = re.compile(r"^(ATM|ITM|OTM)\s*([+-]?\d+)?$", re.IGNORECASE)

# Index-to-derivative instrument type. An index itself is never tradeable.
_OPTION_TYPE_FOR = {
    ExchangeSegment.IDX_I: InstrumentType.OPTIDX,
    ExchangeSegment.NSE_EQ: InstrumentType.OPTSTK,
    ExchangeSegment.BSE_EQ: InstrumentType.OPTSTK,
}
_FUTURE_TYPE_FOR = {
    ExchangeSegment.IDX_I: InstrumentType.FUTIDX,
    ExchangeSegment.NSE_EQ: InstrumentType.FUTSTK,
    ExchangeSegment.BSE_EQ: InstrumentType.FUTSTK,
}


class ResolutionError(RuntimeError):
    """Could not turn a signal into a concrete order. Always fatal for that
    signal — never downgraded into a best-effort guess."""


def parse_strike_spec(spec: str) -> int | float:
    """Return a strike *offset* (int) or an *absolute* strike (float).

    Offsets count steps toward the money: `ITM1` is one strike in-the-money,
    `OTM2` two out. `ATM+1` and `ATM-1` are accepted as synonyms for ITM1/OTM1,
    since both conventions appear in the wild and guessing which one a user
    meant would silently trade the wrong strike.
    """
    spec = str(spec).strip()
    match = _OFFSET_PATTERN.match(spec)
    if match:
        kind = match.group(1).upper()
        amount = int(match.group(2) or 0)
        if kind == "ATM":
            return amount            # ATM+1 == ITM1
        if kind == "ITM":
            return abs(amount) or 1
        return -(abs(amount) or 1)   # OTM
    try:
        value = float(spec)
    except ValueError:
        raise ResolutionError(
            f"cannot parse strike spec {spec!r} — expected ATM, ITM1, OTM2, "
            "ATM+1, or an absolute strike"
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise ResolutionError(f"absolute strike must be positive, got {spec!r}")
    return value


class ExecutionResolver:
    def __init__(self, registry: InstrumentRegistry, plan: TradePlan) -> None:
        self.registry = registry
        self.plan = plan
        self.execution: ExecutionConfig = plan.execution

    # --- validation ------------------------------------------------------
    def validate(self) -> list[str]:
        """Static config checks, run once at startup.

        Catching a bad plan here means it fails before the market opens, rather
        than at 09:20 with a live signal in hand.
        """
        problems: list[str] = []
        cfg = self.execution
        try:
            signal_inst = self._signal_instrument()
        except InstrumentError as exc:
            return [str(exc)]

        if cfg.mode == "same":
            if signal_inst.instrument_type is InstrumentType.INDEX:
                problems.append(
                    f"plan {self.plan.name!r} has mode 'same' on an INDEX "
                    f"({signal_inst.symbol}) — an index cannot be traded. Use "
                    "mode 'option' or 'future'."
                )
            if cfg.quantity is None and cfg.qty_lots < 1:
                problems.append(f"plan {self.plan.name!r} has no quantity configured")

        if cfg.mode in ("option", "future"):
            if not cfg.underlying:
                problems.append(f"plan {self.plan.name!r} mode '{cfg.mode}' needs "
                                "an `underlying`")
            if cfg.on_buy is None:
                problems.append(f"plan {self.plan.name!r} mode '{cfg.mode}' needs "
                                "an `on_buy` leg")
            elif cfg.mode == "option" and not cfg.on_buy.option_type:
                problems.append(f"plan {self.plan.name!r} option leg needs "
                                "`option_type: CE` or `PE`")

        if cfg.mode == "option" and cfg.on_buy is not None:
            try:
                parse_strike_spec(cfg.on_buy.strike)
            except ResolutionError as exc:
                problems.append(str(exc))

        # Prove the contract actually exists today, rather than discovering it
        # when a signal arrives.
        if not problems and cfg.mode in ("option", "future"):
            try:
                self._probe_contract()
            except (ResolutionError, InstrumentError) as exc:
                problems.append(f"plan {self.plan.name!r}: {exc}")

        return problems

    def _probe_contract(self) -> None:
        cfg = self.execution
        itype = self._derivative_type(cfg.mode)
        expiry_rule = cfg.on_buy.expiry if cfg.on_buy else "nearest"
        expiry = self.registry.select_expiry(cfg.underlying, itype, expiry_rule)
        if cfg.mode == "future":
            self.registry.future(cfg.underlying, expiry, instrument_type=itype)
        else:
            ladder = self.registry.strikes(
                cfg.underlying, itype, expiry,
                OptionType(cfg.on_buy.option_type),  # type: ignore[union-attr]
            )
            if not ladder:
                raise ResolutionError(
                    f"no {cfg.on_buy.option_type} strikes for {cfg.underlying} {expiry}"
                )

    # --- resolution ------------------------------------------------------
    def _signal_instrument(self) -> Instrument:
        return self.registry.get(self.plan.signal.exchange_segment,
                                 self.plan.signal.security_id)

    def _derivative_type(self, mode: str) -> InstrumentType:
        segment = self.plan.signal.exchange_segment
        table = _OPTION_TYPE_FOR if mode == "option" else _FUTURE_TYPE_FOR
        itype = table.get(segment)
        if itype is None:
            raise ResolutionError(
                f"cannot derive a {mode} instrument type from segment {segment.value}"
            )
        return itype

    def resolve_entry(self, signal: Signal, *, spot: float | None = None,
                      today: dt.date | None = None) -> OrderIntent:
        """Signal -> the order that opens a position."""
        if signal.action != Action.BUY:
            raise ResolutionError(
                f"resolve_entry called with {signal.action}; SELL means exit "
                "for this strategy"
            )
        cfg = self.execution
        price = spot if spot is not None else signal.price
        if not math.isfinite(price) or price <= 0:
            raise ResolutionError(f"cannot resolve a contract from spot {price!r}")

        if cfg.mode == "same":
            instrument = self._signal_instrument()
            if instrument.instrument_type is InstrumentType.INDEX:
                raise ResolutionError(
                    f"{instrument.symbol} is an index and cannot be traded directly"
                )
        elif cfg.mode == "option":
            instrument = self._resolve_option(cfg.on_buy, price, today=today)
        elif cfg.mode == "future":
            instrument = self._resolve_future(cfg.on_buy, today=today)
        else:
            raise ResolutionError(f"unknown execution mode {cfg.mode!r}")

        return self._build_intent(instrument, TransactionType.BUY, signal=signal)

    def resolve_exit(self, position: Position, *,
                     signal: Signal | None = None) -> OrderIntent:
        """Position -> the order that closes it.

        Quantity comes from the *position*, never from config. Config says how
        much to open; only the broker knows how much is actually held, and
        exiting the configured size when the real size differs would leave a
        residual position or reverse into a short.
        """
        if position.net_qty == 0:
            raise ResolutionError("nothing to exit — position is flat")
        instrument = self.registry.get(position.segment, position.security_id)
        side = (TransactionType.SELL if position.net_qty > 0 else TransactionType.BUY)
        return self._build_intent(instrument, side, signal=signal,
                                  quantity=abs(position.net_qty))

    # --- helpers ---------------------------------------------------------
    def _resolve_option(self, leg: LegConfig | None, spot: float,
                        *, today: dt.date | None = None) -> Instrument:
        cfg = self.execution
        if leg is None or not leg.option_type:
            raise ResolutionError("option mode requires an on_buy leg with option_type")
        itype = self._derivative_type("option")
        option_type = OptionType(leg.option_type)
        expiry = self.registry.select_expiry(cfg.underlying, itype, leg.expiry,
                                             today=today)
        spec = parse_strike_spec(leg.strike)
        if isinstance(spec, float):
            return self.registry.option(cfg.underlying, option_type, spec, expiry,
                                        instrument_type=itype)
        return self.registry.select_option(cfg.underlying, option_type, spot, expiry,
                                           instrument_type=itype, offset=spec)

    def _resolve_future(self, leg: LegConfig | None,
                        *, today: dt.date | None = None) -> Instrument:
        cfg = self.execution
        itype = self._derivative_type("future")
        rule = leg.expiry if leg else "nearest_monthly"
        expiry = self.registry.select_expiry(cfg.underlying, itype, rule, today=today)
        return self.registry.future(cfg.underlying, expiry, instrument_type=itype)

    def quantity_for(self, instrument: Instrument) -> int:
        cfg = self.execution
        if cfg.quantity is not None:
            qty = int(cfg.quantity)
        elif instrument.instrument_type in (OPTION_TYPES | FUTURE_TYPES):
            qty = int(cfg.qty_lots) * int(instrument.lot_size)
        else:
            qty = int(cfg.qty_lots)
        if qty <= 0:
            raise ResolutionError(f"resolved a non-positive quantity ({qty}) for "
                                  f"{instrument.display_name}")
        return qty

    def _build_intent(self, instrument: Instrument, side: TransactionType, *,
                      signal: Signal | None, quantity: int | None = None) -> OrderIntent:
        cfg = self.execution
        qty = quantity if quantity is not None else self.quantity_for(instrument)

        # NSE rejects any single order above the contract's freeze quantity.
        # Better to refuse here with a clear message than to have the exchange
        # bounce it and leave the strategy thinking it is in a position.
        if instrument.freeze_qty and qty > instrument.freeze_qty:
            raise ResolutionError(
                f"quantity {qty} exceeds the exchange freeze limit "
                f"({instrument.freeze_qty}) for {instrument.display_name} — "
                "reduce qty_lots or split the order"
            )

        price = 0.0
        if cfg.order_type in (OrderType.LIMIT, OrderType.STOP_LOSS) and signal:
            price = round(signal.price / instrument.tick_size) * instrument.tick_size

        return OrderIntent(
            plan=self.plan.name,
            instrument=instrument,
            transaction_type=side,
            quantity=qty,
            product_type=cfg.product,
            order_type=cfg.order_type,
            price=price,
            correlation_id=f"orbot-{uuid.uuid4().hex[:18]}",
            signal=signal,
        )


__all__ = ["ExecutionResolver", "ResolutionError", "parse_strike_spec"]
