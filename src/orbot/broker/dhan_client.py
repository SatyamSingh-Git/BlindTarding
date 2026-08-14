"""DhanHQ v2 REST client.

Two things here are not incidental:

1. **Per-route proxying.** Dhan requires a whitelisted static IP for order
   placement / modification / cancellation, but not for data. Order calls go
   through the static-IP proxy; historical and quote calls go direct, because
   proxying them only adds latency and another thing that can break.

2. **Order idempotency.** If `place_order` times out we genuinely do not know
   whether the order reached the exchange, and a blind retry can double a
   position. Every order carries a `correlationId`, and on an ambiguous failure
   we ask Dhan what happened via `/orders/external/{correlation-id}` instead of
   guessing.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from collections import deque
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import httpx

from ..constants import (
    API_BASE,
    HISTORICAL_MAX_DAYS_PER_CALL,
    IST,
    INTRADAY_INTERVALS,
    ExchangeSegment,
    InstrumentType,
    OrderStatus,
    ProductType,
    TransactionType,
)
from ..models import Candle, Instrument, Order, OrderIntent, Position

IST_TZ = ZoneInfo(IST)

_ORDER_ROUTES = ("/orders", "/trades", "/superorder", "/forever")


class DhanAPIError(RuntimeError):
    def __init__(self, message: str, *, code: str = "", status: int = 0,
                 payload: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.payload = payload


class OrderStateUnknown(DhanAPIError):
    """Sent an order but could not confirm its fate. Never auto-retry on this —
    it must be resolved by querying, or escalated to a human."""


class _RateLimiter:
    """Sliding-window limiter. Dhan allows 25 orders/sec and 7000/day; we run
    far below both, so this exists to catch runaway loops, not to ride the cap."""

    def __init__(self, per_second: int, per_day: int) -> None:
        self.per_second = per_second
        self.per_day = per_day
        self._recent: deque[float] = deque()
        self._day_count = 0
        self._day = dt.date.today()

    def check(self) -> None:
        now = time.monotonic()
        today = dt.date.today()
        if today != self._day:
            self._day, self._day_count = today, 0
        while self._recent and now - self._recent[0] > 1.0:
            self._recent.popleft()
        if len(self._recent) >= self.per_second:
            raise DhanAPIError(
                f"local order rate limit hit ({self.per_second}/sec) — refusing to send"
            )
        if self._day_count >= self.per_day:
            raise DhanAPIError(f"local daily order cap hit ({self.per_day}) — refusing to send")
        self._recent.append(now)
        self._day_count += 1


class DhanClient:
    def __init__(
        self,
        client_id: str,
        access_token: str,
        *,
        order_proxy: str | None = None,
        proxy_data_calls: bool = False,
        timeout: float = 10.0,
        max_orders_per_second: int = 5,
        max_orders_per_day: int = 500,
    ) -> None:
        self.client_id = str(client_id)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "access-token": access_token,
            "client-id": self.client_id,
        }
        self._data = httpx.Client(
            base_url=API_BASE, headers=headers, timeout=timeout,
            proxy=order_proxy if (proxy_data_calls and order_proxy) else None,
        )
        self._orders = (
            httpx.Client(base_url=API_BASE, headers=headers, timeout=timeout,
                         proxy=order_proxy)
            if order_proxy else self._data
        )
        self.has_static_ip_proxy = bool(order_proxy)
        self._limiter = _RateLimiter(max_orders_per_second, max_orders_per_day)

    def close(self) -> None:
        self._orders.close()
        if self._orders is not self._data:
            self._data.close()

    def __enter__(self) -> DhanClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- transport ------------------------------------------------------
    def _client_for(self, path: str) -> httpx.Client:
        return self._orders if path.startswith(_ORDER_ROUTES) else self._data

    def _request(self, method: str, path: str, *, json: Any = None,
                 retries: int = 2) -> Any:
        client = self._client_for(path)
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = client.request(method, path, json=json)
            except httpx.RequestError as exc:
                last = exc
                if attempt < retries:
                    time.sleep(0.4 * (2 ** attempt))
                    continue
                raise DhanAPIError(f"{method} {path} failed: {exc}") from exc

            if resp.status_code >= 500 and attempt < retries:
                time.sleep(0.4 * (2 ** attempt))
                continue
            return self._unwrap(resp, method, path)
        raise DhanAPIError(f"{method} {path} exhausted retries: {last}")

    @staticmethod
    def _unwrap(resp: httpx.Response, method: str, path: str) -> Any:
        try:
            body = resp.json()
        except ValueError:
            body = resp.text

        if resp.status_code >= 400:
            code, msg = "", str(body)
            if isinstance(body, dict):
                code = str(body.get("errorCode") or body.get("errorType") or "")
                msg = str(body.get("errorMessage") or body.get("message") or body)
            raise DhanAPIError(f"{method} {path} -> {resp.status_code}: {msg}",
                               code=code, status=resp.status_code, payload=body)
        # Dhan sometimes returns 200 with an embedded error envelope.
        if isinstance(body, dict) and body.get("status") == "failure":
            raise DhanAPIError(f"{method} {path}: {body}",
                               code=str(body.get("errorCode", "")), payload=body)
        return body

    # --- account --------------------------------------------------------
    def fund_limits(self) -> dict[str, Any]:
        return self._request("GET", "/fundlimit")

    def available_balance(self) -> float:
        funds = self.fund_limits()
        for key in ("availabelBalance", "availableBalance", "withdrawableBalance"):
            if key in funds:
                return float(funds[key])
        raise DhanAPIError(f"no balance field in fund limits: {funds}")

    def positions(self) -> list[Position]:
        rows = self._request("GET", "/positions") or []
        return [_parse_position(r) for r in rows]

    def holdings(self) -> list[dict[str, Any]]:
        return self._request("GET", "/holdings") or []

    # --- orders ---------------------------------------------------------
    def place_order(self, intent: OrderIntent) -> Order:
        """Place an order. Raises `OrderStateUnknown` if the outcome is
        genuinely ambiguous — the caller must resolve, not retry."""
        if not intent.correlation_id:
            intent.correlation_id = f"orbot-{uuid.uuid4().hex[:18]}"
        self._limiter.check()
        payload = intent.to_payload(self.client_id)

        try:
            # retries=0: a network-level retry on a POST /orders can double a position.
            body = self._request("POST", "/orders", json=payload, retries=0)
        except DhanAPIError as exc:
            if exc.status and exc.status < 500:
                raise  # a clean rejection — the order definitively did not go through
            resolved = self.find_order_by_correlation(intent.correlation_id)
            if resolved is not None:
                return resolved
            raise OrderStateUnknown(
                f"order {intent.correlation_id} outcome unknown after {exc}. "
                "Resolve manually before sending anything else.",
                code=exc.code, status=exc.status,
            ) from exc

        return Order(
            order_id=str(body.get("orderId", "")),
            status=_parse_status(body.get("orderStatus")),
            security_id=intent.instrument.security_id,
            segment=intent.instrument.segment,
            transaction_type=intent.transaction_type,
            quantity=intent.quantity,
            price=intent.price,
            correlation_id=intent.correlation_id,
            raw=body if isinstance(body, dict) else {},
        )

    def modify_order(self, order_id: str, **fields: Any) -> dict[str, Any]:
        payload = {"dhanClientId": self.client_id, "orderId": str(order_id), **fields}
        return self._request("PUT", f"/orders/{order_id}", json=payload, retries=0)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/orders/{order_id}", retries=1)

    def get_order(self, order_id: str) -> Order:
        body = self._request("GET", f"/orders/{order_id}")
        row = body[0] if isinstance(body, list) and body else body
        return _parse_order(row)

    def find_order_by_correlation(self, correlation_id: str) -> Order | None:
        try:
            body = self._request("GET", f"/orders/external/{correlation_id}", retries=1)
        except DhanAPIError:
            return None
        row = body[0] if isinstance(body, list) and body else body
        return _parse_order(row) if isinstance(row, dict) and row.get("orderId") else None

    def orders(self) -> list[Order]:
        return [_parse_order(r) for r in (self._request("GET", "/orders") or [])]

    def trades(self) -> list[dict[str, Any]]:
        return self._request("GET", "/trades") or []

    # --- historical -----------------------------------------------------
    def intraday(
        self,
        instrument: Instrument,
        interval: int,
        start: dt.datetime,
        end: dt.datetime,
        *,
        oi: bool = False,
    ) -> list[Candle]:
        """Intraday candles. Dhan caps each call at 90 days, so longer ranges
        are chunked and stitched."""
        if interval not in INTRADAY_INTERVALS:
            raise ValueError(f"interval must be one of {INTRADAY_INTERVALS}, got {interval}")

        out: list[Candle] = []
        for chunk_start, chunk_end in _chunk_range(start, end, HISTORICAL_MAX_DAYS_PER_CALL):
            body = self._request("POST", "/charts/intraday", json={
                "securityId": instrument.security_id,
                "exchangeSegment": instrument.segment.value,
                "instrument": instrument.instrument_type.value,
                "interval": int(interval),
                "oi": oi,
                "fromDate": chunk_start.strftime("%Y-%m-%d %H:%M:%S"),
                "toDate": chunk_end.strftime("%Y-%m-%d %H:%M:%S"),
            })
            out.extend(_parse_candles(body))
        return _dedupe_candles(out)

    def daily(
        self, instrument: Instrument, start: dt.date, end: dt.date, *, oi: bool = False,
    ) -> list[Candle]:
        body = self._request("POST", "/charts/historical", json={
            "securityId": instrument.security_id,
            "exchangeSegment": instrument.segment.value,
            "instrument": instrument.instrument_type.value,
            "oi": oi,
            "fromDate": start.strftime("%Y-%m-%d"),
            "toDate": end.strftime("%Y-%m-%d"),
        })
        return _parse_candles(body)


# --- parsing helpers ----------------------------------------------------
def _parse_status(raw: Any) -> OrderStatus:
    try:
        return OrderStatus(str(raw).upper())
    except ValueError:
        return OrderStatus.TRANSIT


def _first_float(row: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for k in keys:
        if row.get(k) not in (None, ""):
            try:
                return float(row[k])
            except (TypeError, ValueError):
                continue
    return default


def _first_int(row: dict[str, Any], *keys: str, default: int = 0) -> int:
    return int(_first_float(row, *keys, default=default))


def _parse_order(row: dict[str, Any]) -> Order:
    segment = None
    raw_seg = row.get("exchangeSegment")
    if raw_seg:
        try:
            segment = ExchangeSegment(str(raw_seg))
        except ValueError:
            segment = None
    txn = None
    if row.get("transactionType"):
        try:
            txn = TransactionType(str(row["transactionType"]).upper())
        except ValueError:
            txn = None
    return Order(
        order_id=str(row.get("orderId", "")),
        status=_parse_status(row.get("orderStatus")),
        security_id=str(row.get("securityId", "")),
        segment=segment,
        transaction_type=txn,
        quantity=_first_int(row, "quantity", "orderQuantity"),
        filled_qty=_first_int(row, "filledQty", "filled_qty", "tradedQuantity"),
        avg_price=_first_float(row, "averageTradedPrice", "avgPrice", "tradedPrice"),
        price=_first_float(row, "price"),
        correlation_id=str(row.get("correlationId", "") or ""),
        raw=row,
    )


def _parse_position(row: dict[str, Any]) -> Position:
    product = None
    if row.get("productType"):
        try:
            product = ProductType(str(row["productType"]).upper())
        except ValueError:
            product = None
    return Position(
        security_id=str(row.get("securityId", "")),
        segment=ExchangeSegment(str(row.get("exchangeSegment", "NSE_EQ"))),
        net_qty=_first_int(row, "netQty", "netQuantity"),
        avg_price=_first_float(row, "buyAvg", "costPrice", "averagePrice"),
        realised=_first_float(row, "realizedProfit", "realisedProfit"),
        unrealised=_first_float(row, "unrealizedProfit", "unrealisedProfit"),
        product_type=product,
        raw=row,
    )


def _parse_candles(body: Any) -> list[Candle]:
    """Dhan returns column arrays, not row objects.

    NOTE (validate in Phase 1): the epoch is interpreted as UTC here. Dhan has
    historically been inconsistent about this. Before trusting any signal, check
    a known bar against the Dhan chart — a silent 5h30m shift would break every
    session-based rule in the ORB logic.
    """
    if not isinstance(body, dict):
        return []
    ts = body.get("timestamp") or []
    o, h, low, c = (body.get(k) or [] for k in ("open", "high", "low", "close"))
    vol = body.get("volume") or []
    oi = body.get("open_interest") or []
    n = min(len(ts), len(o), len(h), len(low), len(c))

    out: list[Candle] = []
    for i in range(n):
        out.append(Candle(
            ts=dt.datetime.fromtimestamp(int(ts[i]), tz=dt.timezone.utc).astimezone(IST_TZ),
            open=float(o[i]), high=float(h[i]), low=float(low[i]), close=float(c[i]),
            volume=int(vol[i]) if i < len(vol) else 0,
            oi=int(oi[i]) if i < len(oi) else None,
            closed=True,
        ))
    return out


def _dedupe_candles(candles: Sequence[Candle]) -> list[Candle]:
    """Chunk boundaries can overlap by a bar."""
    seen: dict[dt.datetime, Candle] = {}
    for candle in candles:
        seen[candle.ts] = candle
    return [seen[k] for k in sorted(seen)]


def _chunk_range(
    start: dt.datetime, end: dt.datetime, max_days: int,
) -> list[tuple[dt.datetime, dt.datetime]]:
    out: list[tuple[dt.datetime, dt.datetime]] = []
    cursor = start
    span = dt.timedelta(days=max_days)
    while cursor < end:
        stop = min(cursor + span, end)
        out.append((cursor, stop))
        cursor = stop
    return out or [(start, end)]


__all__ = [
    "DhanClient",
    "DhanAPIError",
    "OrderStateUnknown",
    "InstrumentType",
]
