"""Broker selection.

Both adapters expose the same method names — `intraday`, `daily`, `place_order`,
`positions`, `available_balance`, `get_order`, `cancel_order` — so everything
above the broker layer is already duck-type compatible and needs no changes to
switch. This module just assembles the right set.

Keeping both adapters alive is a hedge, not indecision: if one broker's API
degrades during expiry-day volatility — exactly when a stop has to work —
switching is a config line rather than a rewrite and a re-validation.

**Switching brokers invalidates every configured `security_id`.** Dhan's
securityId and Angel's token are different numbers for the same contract, so a
plan carrying `security_id: "13"` (Dhan's NIFTY) means something else entirely
on Angel. `orbot validate` refuses to run when the configured broker does not
match the ids the plans were written against.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import structlog

from ..config import Settings
from ..models import Tick
from .instruments import InstrumentRegistry

log = structlog.get_logger(__name__)

BrokerName = Literal["dhan", "angel"]

# Well-known index ids, used to catch a broker/config mismatch before trading.
# Dhan NIFTY is 13; Angel's NIFTY 50 token is 99926000.
_DHAN_INDEX_IDS = {"13", "25", "51"}


@dataclass(slots=True)
class BrokerBundle:
    """Everything the engine needs from one broker."""

    name: str
    client: object                      # REST: data + orders
    registry: InstrumentRegistry
    make_feed: Callable[[Callable[[Tick], None]], object]
    token_status: Callable[[], str]
    native_intervals: tuple[int, ...]
    supports_correlation_id: bool

    @property
    def has_static_ip_proxy(self) -> bool:
        return bool(getattr(self.client, "has_static_ip_proxy", False))


def _dhan_bundle(settings: Settings, underlyings: list[str] | None) -> BrokerBundle:
    from ..constants import INTRADAY_INTERVALS
    from .auth import TokenManager
    from .dhan_client import DhanClient
    from .dhan_feed import DhanFeed, FeedInstrument
    from .instruments import download_scrip_master, is_fresh

    tokens = TokenManager(
        settings.dhan_client_id, static_token=settings.dhan_access_token,
        pin=settings.dhan_pin, totp_secret=settings.dhan_totp_secret,
        cache_path=settings.token_cache_path,
    )
    path = settings.scrip_master_path
    if not is_fresh(path):
        log.info("broker.refreshing_scrip_master", broker="dhan")
        download_scrip_master(path)
    registry = InstrumentRegistry.load(path, underlyings=underlyings)

    client = DhanClient(
        settings.dhan_client_id, tokens,
        order_proxy=settings.dhan_order_proxy or None,
        proxy_data_calls=settings.proxy_data_calls,
        max_orders_per_second=settings.max_orders_per_second,
        max_orders_per_day=settings.max_orders_per_day,
    )

    def make_feed(on_tick):
        feed = DhanFeed(settings.dhan_client_id, tokens.token, on_tick=on_tick)
        feed._orbot_instrument = FeedInstrument      # used by the engine to subscribe
        return feed

    return BrokerBundle(
        name="dhan", client=client, registry=registry, make_feed=make_feed,
        token_status=tokens.describe, native_intervals=INTRADAY_INTERVALS,
        supports_correlation_id=True,
    )


def _angel_bundle(settings: Settings, underlyings: list[str] | None) -> BrokerBundle:
    from .angel.auth import AngelAuth
    from .angel.client import NATIVE_INTERVALS, AngelClient
    from .angel.feed import AngelFeed, AngelFeedInstrument
    from .angel.instruments import download_scrip_master, load_registry

    auth = AngelAuth(
        settings.angel_client_code, settings.angel_api_key,
        mpin=settings.angel_mpin, totp_secret=settings.angel_totp_secret,
        cache_path=settings.angel_session_path,
    )
    path = settings.angel_scrip_master_path
    if not _is_fresh(path):
        log.info("broker.refreshing_scrip_master", broker="angel")
        download_scrip_master(path)
    registry = load_registry(path, underlyings=underlyings)

    client = AngelClient(
        auth, order_proxy=settings.dhan_order_proxy or None,
        proxy_data_calls=settings.proxy_data_calls,
        max_orders_per_second=settings.max_orders_per_second,
        max_orders_per_day=settings.max_orders_per_day,
    )

    def make_feed(on_tick):
        feed = AngelFeed(auth, on_tick=on_tick)
        feed._orbot_instrument = AngelFeedInstrument
        return feed

    return BrokerBundle(
        name="angel", client=client, registry=registry, make_feed=make_feed,
        token_status=auth.describe, native_intervals=NATIVE_INTERVALS,
        # Angel has no correlation id, so an ambiguous placement cannot be
        # resolved by lookup — the OMS escalates instead of guessing.
        supports_correlation_id=False,
    )


def _is_fresh(path: Path) -> bool:
    import datetime as dt
    if not path.exists():
        return False
    return dt.date.fromtimestamp(path.stat().st_mtime) >= dt.date.today()


def build_broker(settings: Settings, *,
                 underlyings: list[str] | None = None) -> BrokerBundle:
    if settings.broker == "angel":
        return _angel_bundle(settings, underlyings)
    return _dhan_bundle(settings, underlyings)


def check_plan_ids(broker: str, security_ids: list[str]) -> list[str]:
    """Catch plans still carrying the other broker's instrument ids.

    Dhan's securityId and Angel's token are different numbers for the same
    contract, so a stale id does not error — it resolves to a real but unrelated
    instrument. That is a wrong trade that looks entirely normal.
    """
    problems: list[str] = []
    dhan_shaped = [i for i in security_ids if i in _DHAN_INDEX_IDS]
    if broker == "angel" and dhan_shaped:
        problems.append(
            f"security_id {', '.join(dhan_shaped)} look like Dhan ids, but the "
            "configured broker is Angel. Angel uses different tokens for the "
            "same contracts (NIFTY is 99926000, not 13) — re-resolve every plan "
            "with `orbot resolve` before trading."
        )
    if broker == "dhan":
        angel_shaped = [i for i in security_ids if len(i) >= 8 and i.startswith("999")]
        if angel_shaped:
            problems.append(
                f"security_id {', '.join(angel_shaped)} look like Angel tokens, "
                "but the configured broker is Dhan. Re-resolve every plan."
            )
    return problems


__all__ = ["build_broker", "BrokerBundle", "BrokerName", "check_plan_ids"]
