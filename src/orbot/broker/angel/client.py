"""Angel One SmartAPI REST client.

Mirrors the Dhan client's contract so everything above the broker layer is
unchanged. Two places where Angel genuinely differs, both of which matter:

**No correlation id.** Dhan lets you stamp an order with your own id and later
ask "what happened to this one?". Angel has no equivalent, so an ambiguous
placement — the request timed out, the order may or may not have reached the
exchange — has to be resolved by scanning the order book for a matching order
placed in the last few seconds. That is a heuristic where Dhan has a guarantee,
so the match is deliberately strict and anything short of one unambiguous hit
raises `OrderStateUnknown` and halts the bot rather than guessing.

**Timestamps are unambiguous.** Angel returns ISO strings with an explicit
`+05:30` offset, so the 5h30m question that hangs over Dhan's bare epochs does
not arise here.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import httpx
import structlog

from ...constants import (
    IST,
    ExchangeSegment,
    OrderStatus,
    OrderType,
    ProductType,
    TransactionType,
)
from ...models import Candle, Instrument, Order, OrderIntent, Position
from ..dhan_client import DhanAPIError, OrderStateUnknown, _RateLimiter
from .auth import API_BASE, AngelAuth, AngelAuthError
from .instruments import exchange_name

log = structlog.get_logger(__name__)
IST_TZ = ZoneInfo(IST)

PLACE = "/rest/secure/angelbroking/order/v1/placeOrder"
MODIFY = "/rest/secure/angelbroking/order/v1/modifyOrder"
CANCEL = "/rest/secure/angelbroking/order/v1/cancelOrder"
ORDER_BOOK = "/rest/secure/angelbroking/order/v1/getOrderBook"
POSITIONS = "/rest/secure/angelbroking/order/v1/getPosition"
RMS = "/rest/secure/angelbroking/user/v1/getRMS"
CANDLES = "/rest/secure/angelbroking/historical/v1/getCandleData"
LTP = "/rest/secure/angelbroking/order/v1/getLtpData"

# Angel aggregates these itself; anything else we build by resampling.
NATIVE_INTERVALS = (1, 3, 5, 10, 15, 30, 60)

_INTERVAL_NAMES = {
    1: "ONE_MINUTE", 3: "THREE_MINUTE", 5: "FIVE_MINUTE", 10: "TEN_MINUTE",
    15: "FIFTEEN_MINUTE", 30: "THIRTY_MINUTE", 60: "ONE_HOUR",
}

# Angel caps each historical request by interval. Exceeding it returns an error
# rather than a truncated result, so requests are chunked to stay inside.
MAX_DAYS_PER_CALL = {
    1: 30, 3: 60, 5: 100, 10: 100, 15: 200, 30: 200, 60: 400,
}
MAX_DAILY_DAYS = 2000

_PRODUCT_NAMES = {
    ProductType.INTRADAY: "INTRADAY",
    ProductType.CNC: "DELIVERY",
    ProductType.MARGIN: "CARRYFORWARD",
    ProductType.MTF: "DELIVERY",
    ProductType.CO: "CARRYFORWARD",
    ProductType.BO: "CARRYFORWARD",
}
_ORDER_TYPE_NAMES = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.STOP_LOSS: "STOPLOSS_LIMIT",
    OrderType.STOP_LOSS_MARKET: "STOPLOSS_MARKET",
}
_STATUS_MAP = {
    "complete": OrderStatus.TRADED,
    "rejected": OrderStatus.REJECTED,
    "cancelled": OrderStatus.CANCELLED,
    "canceled": OrderStatus.CANCELLED,
    "open": OrderStatus.PENDING,
    "open pending": OrderStatus.PENDING,
    "validation pending": OrderStatus.TRANSIT,
    "put order req received": OrderStatus.TRANSIT,
    "trigger pending": OrderStatus.TRIGGERED,
    "modify pending": OrderStatus.PENDING,
    "modified": OrderStatus.PENDING,
    "cancelled after market order": OrderStatus.CANCELLED,
}


def _is_json(resp: httpx.Response) -> bool:
    """Whether a response carries a JSON body at all.

    Used to tell Angel's rate-limit 403 (HTML) from a genuine authorisation
    403 (JSON): the first is worth retrying, the second never is.
    """
    try:
        resp.json()
    except ValueError:
        return False
    return True


class AngelClient:
    def __init__(
        self,
        auth: AngelAuth,
        *,
        order_proxy: str | None = None,
        proxy_data_calls: bool = False,
        timeout: float = 10.0,
        max_orders_per_second: int = 5,
        max_orders_per_day: int = 500,
    ) -> None:
        self.auth = auth
        self.client_id = auth.client_code
        self._timeout = timeout
        self._data = httpx.Client(
            base_url=API_BASE, timeout=timeout,
            proxy=order_proxy if (proxy_data_calls and order_proxy) else None)
        self._orders = (httpx.Client(base_url=API_BASE, timeout=timeout,
                                     proxy=order_proxy)
                        if order_proxy else self._data)
        self.has_static_ip_proxy = bool(order_proxy)
        self._limiter = _RateLimiter(max_orders_per_second, max_orders_per_day)

    def close(self) -> None:
        self._orders.close()
        if self._orders is not self._data:
            self._data.close()

    def __enter__(self) -> AngelClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def native_intervals(self) -> tuple[int, ...]:
        return NATIVE_INTERVALS

    # --- transport -------------------------------------------------------
    def _call(self, method: str, path: str, *, json: Any = None,
              retries: int = 2, order_route: bool = False) -> Any:
        client = self._orders if order_route else self._data
        try:
            headers = self.auth.auth_headers()
        except AngelAuthError as exc:
            raise DhanAPIError(f"no usable Angel session: {exc}") from exc

        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = client.request(method, path, json=json, headers=headers)
            except httpx.RequestError as exc:
                last = exc
                if attempt < retries:
                    time.sleep(0.4 * (2 ** attempt))
                    continue
                raise DhanAPIError(f"{method} {path} failed: {exc}") from exc
            if resp.status_code >= 500 and attempt < retries:
                time.sleep(0.4 * (2 ** attempt))
                continue
            # Angel answers a breached rate limit with 403 and an HTML body,
            # where a real authorisation failure comes back as JSON. Retrying
            # the former matters: an unlucky burst during warmup used to kill
            # the engine outright, before it had placed a single order.
            if (resp.status_code in (403, 429) and attempt < retries
                    and not _is_json(resp)):
                time.sleep(1.5 * (2 ** attempt))
                continue
            return self._unwrap(resp, method, path)
        raise DhanAPIError(f"{method} {path} exhausted retries: {last}")

    @staticmethod
    def _unwrap(resp: httpx.Response, method: str, path: str) -> Any:
        try:
            body = resp.json()
        except ValueError:
            raise DhanAPIError(f"{method} {path} returned non-JSON "
                               f"({resp.status_code})", status=resp.status_code)
        if resp.status_code >= 400:
            raise DhanAPIError(f"{method} {path} -> {resp.status_code}: "
                               f"{_message(body)}", status=resp.status_code,
                               payload=body)
        # Angel signals application errors with status=false and HTTP 200.
        if isinstance(body, dict) and body.get("status") is False:
            raise DhanAPIError(f"{method} {path}: {_message(body)}",
                               code=str(body.get("errorcode", "")), payload=body)
        return body.get("data") if isinstance(body, dict) else body

    # --- account ---------------------------------------------------------
    def available_balance(self) -> float:
        data = self._call("GET", RMS) or {}
        for key in ("availablecash", "net", "availableintradaypayin"):
            if data.get(key) not in (None, ""):
                try:
                    return float(data[key])
                except (TypeError, ValueError):
                    continue
        raise DhanAPIError(f"no balance field in Angel RMS response: {data}")

    def positions(self) -> list[Position]:
        rows = self._call("GET", POSITIONS) or []
        return [_parse_position(r) for r in rows if isinstance(r, dict)]

    # --- orders ----------------------------------------------------------
    def place_order(self, intent: OrderIntent) -> Order:
        if not intent.correlation_id:
            intent.correlation_id = f"orbot-{uuid.uuid4().hex[:18]}"
        self._limiter.check()

        payload = {
            "variety": "NORMAL",
            "tradingsymbol": intent.instrument.display_name,
            "symboltoken": intent.instrument.security_id,
            "transactiontype": intent.transaction_type.value,
            "exchange": exchange_name(intent.instrument.segment),
            "ordertype": _ORDER_TYPE_NAMES[intent.order_type],
            "producttype": _PRODUCT_NAMES.get(intent.product_type, "INTRADAY"),
            "duration": intent.validity.value,
            "price": str(round(float(intent.price), 2)),
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(int(intent.quantity)),
        }
        if intent.order_type in (OrderType.STOP_LOSS, OrderType.STOP_LOSS_MARKET):
            payload["triggerprice"] = str(round(float(intent.trigger_price), 2))

        placed_at = dt.datetime.now(IST_TZ)
        try:
            # retries=0: a network retry on a placement can double a position.
            data = self._call("POST", PLACE, json=payload, retries=0,
                              order_route=True) or {}
        except DhanAPIError as exc:
            if exc.status and exc.status < 500:
                raise          # a clean rejection: the order did not go through
            resolved = self._find_recent_order(intent, placed_at)
            if resolved is not None:
                return resolved
            raise OrderStateUnknown(
                f"Angel order for {intent.instrument.display_name} may or may "
                f"not have reached the exchange after {exc}. Angel has no "
                "correlation id, so this cannot be resolved automatically — "
                "check the order book manually before restarting.",
                status=exc.status,
            ) from exc

        return Order(
            order_id=str(data.get("orderid", "")),
            status=OrderStatus.TRANSIT,
            security_id=intent.instrument.security_id,
            segment=intent.instrument.segment,
            transaction_type=intent.transaction_type,
            quantity=intent.quantity,
            price=intent.price,
            correlation_id=intent.correlation_id,
            raw=data if isinstance(data, dict) else {},
        )

    def _find_recent_order(self, intent: OrderIntent,
                           placed_at: dt.datetime) -> Order | None:
        """Best-effort recovery of an ambiguous placement.

        Strict on purpose. Angel gives no id to match on, so this looks for
        exactly one order on the same token, side and quantity in the seconds
        around the attempt. Two candidates, or none, means we still do not know
        — and "do not know" must escalate, not guess.
        """
        try:
            rows = self._call("GET", ORDER_BOOK, retries=1) or []
        except DhanAPIError:
            return None

        window_start = placed_at - dt.timedelta(seconds=90)
        matches = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("symboltoken", "")) != intent.instrument.security_id:
                continue
            if str(row.get("transactiontype", "")).upper() != \
                    intent.transaction_type.value:
                continue
            if int(_num(row.get("quantity"), 0)) != int(intent.quantity):
                continue
            stamped = _parse_angel_time(row.get("updatetime") or row.get("exchtime"))
            if stamped is not None and stamped < window_start:
                continue
            matches.append(row)

        if len(matches) != 1:
            log.warning("angel.ambiguous_order_recovery", candidates=len(matches),
                        token=intent.instrument.security_id)
            return None
        order = _parse_order(matches[0])
        order.correlation_id = intent.correlation_id
        log.warning("angel.recovered_order", order_id=order.order_id,
                    detail="matched by token/side/quantity, not by id")
        return order

    def modify_order(self, order_id: str, **fields: Any) -> dict:
        payload = {"variety": "NORMAL", "orderid": str(order_id), **fields}
        return self._call("POST", MODIFY, json=payload, retries=0,
                          order_route=True) or {}

    def cancel_order(self, order_id: str) -> dict:
        return self._call("POST", CANCEL,
                          json={"variety": "NORMAL", "orderid": str(order_id)},
                          retries=1, order_route=True) or {}

    def get_order(self, order_id: str) -> Order:
        for row in self.orders_raw():
            if str(row.get("orderid", "")) == str(order_id):
                return _parse_order(row)
        raise DhanAPIError(f"order {order_id} not found in the Angel order book")

    def orders_raw(self) -> list[dict]:
        return [r for r in (self._call("GET", ORDER_BOOK) or [])
                if isinstance(r, dict)]

    def orders(self) -> list[Order]:
        return [_parse_order(r) for r in self.orders_raw()]

    def find_order_by_correlation(self, correlation_id: str) -> Order | None:
        """Angel has no correlation id. Returning None keeps the caller honest:
        an ambiguous placement escalates rather than silently resolving."""
        return None

    def ltp(self, instrument: Instrument) -> float | None:
        """Snapshot price. Used to cross-check the feed's paise scaling."""
        payload = {
            "exchange": exchange_name(instrument.segment),
            "tradingsymbol": instrument.display_name,
            "symboltoken": instrument.security_id,
        }
        try:
            data = self._call("POST", LTP, json=payload, retries=1) or {}
        except DhanAPIError:
            return None
        value = _num(data.get("ltp"), 0.0)
        return value or None

    # --- historical ------------------------------------------------------
    def intraday(self, instrument: Instrument, interval: int,
                 start: dt.datetime, end: dt.datetime, *,
                 oi: bool = False) -> list[Candle]:
        if interval not in _INTERVAL_NAMES:
            raise ValueError(f"Angel serves {tuple(_INTERVAL_NAMES)}, got {interval}")
        span = MAX_DAYS_PER_CALL.get(interval, 30)
        out: list[Candle] = []
        for chunk_start, chunk_end in _chunk(start, end, span):
            rows = self._call("POST", CANDLES, json={
                "exchange": exchange_name(instrument.segment),
                "symboltoken": instrument.security_id,
                "interval": _INTERVAL_NAMES[interval],
                "fromdate": chunk_start.strftime("%Y-%m-%d %H:%M"),
                "todate": chunk_end.strftime("%Y-%m-%d %H:%M"),
            }) or []
            out.extend(_parse_candles(rows))
        return _dedupe(out)

    def daily(self, instrument: Instrument, start: dt.date, end: dt.date, *,
              oi: bool = False) -> list[Candle]:
        out: list[Candle] = []
        begin = dt.datetime.combine(start, dt.time(9, 0))
        finish = dt.datetime.combine(end, dt.time(15, 30))
        for chunk_start, chunk_end in _chunk(begin, finish, MAX_DAILY_DAYS):
            rows = self._call("POST", CANDLES, json={
                "exchange": exchange_name(instrument.segment),
                "symboltoken": instrument.security_id,
                "interval": "ONE_DAY",
                "fromdate": chunk_start.strftime("%Y-%m-%d %H:%M"),
                "todate": chunk_end.strftime("%Y-%m-%d %H:%M"),
            }) or []
            out.extend(_parse_candles(rows))
        return _dedupe(out)


# --- parsing -------------------------------------------------------------
def _num(raw: object, default: float = 0.0) -> float:
    try:
        return float(str(raw))
    except (TypeError, ValueError):
        return default


def _message(body: object) -> str:
    if isinstance(body, dict):
        return str(body.get("message") or body.get("errorcode") or body)[:300]
    return str(body)[:300]


def _parse_angel_time(raw: object) -> dt.datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S"):
        try:
            return dt.datetime.strptime(text, fmt).replace(tzinfo=IST_TZ)
        except ValueError:
            continue
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=IST_TZ) if parsed.tzinfo is None else parsed


def _parse_status(raw: object) -> OrderStatus:
    return _STATUS_MAP.get(str(raw or "").strip().lower(), OrderStatus.TRANSIT)


def _parse_order(row: dict) -> Order:
    segment = None
    exchange = str(row.get("exchange", "")).upper()
    for seg, name in {ExchangeSegment.NSE_FNO: "NFO", ExchangeSegment.NSE_EQ: "NSE",
                      ExchangeSegment.BSE_EQ: "BSE", ExchangeSegment.BSE_FNO: "BFO",
                      ExchangeSegment.MCX_COMM: "MCX",
                      ExchangeSegment.NSE_CURRENCY: "CDS"}.items():
        if exchange == name:
            segment = seg
            break
    txn = None
    raw_txn = str(row.get("transactiontype", "")).upper()
    if raw_txn in ("BUY", "SELL"):
        txn = TransactionType(raw_txn)

    return Order(
        order_id=str(row.get("orderid", "")),
        status=_parse_status(row.get("status") or row.get("orderstatus")),
        security_id=str(row.get("symboltoken", "")),
        segment=segment,
        transaction_type=txn,
        quantity=int(_num(row.get("quantity"), 0)),
        filled_qty=int(_num(row.get("filledshares"), 0)),
        avg_price=_num(row.get("averageprice"), 0.0),
        price=_num(row.get("price"), 0.0),
        correlation_id="",
        raw=row,
    )


def _parse_position(row: dict) -> Position:
    exchange = str(row.get("exchange", "")).upper()
    segment = {"NFO": ExchangeSegment.NSE_FNO, "NSE": ExchangeSegment.NSE_EQ,
               "BSE": ExchangeSegment.BSE_EQ, "BFO": ExchangeSegment.BSE_FNO,
               "MCX": ExchangeSegment.MCX_COMM,
               "CDS": ExchangeSegment.NSE_CURRENCY}.get(
                   exchange, ExchangeSegment.NSE_EQ)
    product = None
    raw_product = str(row.get("producttype", "")).upper()
    if raw_product == "INTRADAY":
        product = ProductType.INTRADAY
    elif raw_product == "DELIVERY":
        product = ProductType.CNC
    elif raw_product == "CARRYFORWARD":
        product = ProductType.MARGIN

    return Position(
        security_id=str(row.get("symboltoken", "")),
        segment=segment,
        net_qty=int(_num(row.get("netqty"), 0.0)),
        avg_price=_num(row.get("avgnetprice") or row.get("netprice"), 0.0),
        realised=_num(row.get("realised") or row.get("pnl"), 0.0),
        unrealised=_num(row.get("unrealised"), 0.0),
        product_type=product,
        raw=row,
    )


def _parse_candles(rows: object) -> list[Candle]:
    """Angel returns `[timestamp, open, high, low, close, volume]` per bar, with
    an explicit `+05:30` offset — no epoch ambiguity to resolve."""
    out: list[Candle] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        stamp = _parse_angel_time(row[0])
        if stamp is None:
            continue
        out.append(Candle(
            ts=stamp.astimezone(IST_TZ),
            open=_num(row[1]), high=_num(row[2]),
            low=_num(row[3]), close=_num(row[4]),
            volume=int(_num(row[5], 0.0)), closed=True,
        ))
    return out


def _dedupe(candles: Sequence[Candle]) -> list[Candle]:
    seen: dict[dt.datetime, Candle] = {c.ts: c for c in candles}
    return [seen[k] for k in sorted(seen)]


def _chunk(start: dt.datetime, end: dt.datetime,
           max_days: int) -> list[tuple[dt.datetime, dt.datetime]]:
    out: list[tuple[dt.datetime, dt.datetime]] = []
    cursor, span = start, dt.timedelta(days=max_days)
    while cursor < end:
        stop = min(cursor + span, end)
        out.append((cursor, stop))
        cursor = stop
    return out or [(start, end)]


__all__ = ["AngelClient", "NATIVE_INTERVALS", "MAX_DAYS_PER_CALL"]
