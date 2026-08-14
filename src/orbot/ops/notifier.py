"""Telegram notifications.

An unattended bot that fails silently is worse than one that does not run at
all. Every order, every guard trip and every crash goes here.

Notification failures are swallowed on purpose. Telegram being unreachable must
never propagate into the trading path — a failed message is an annoyance, an
exception thrown while flattening a position is a real loss. Failures are logged
and counted so a persistently broken notifier is still visible.
"""

from __future__ import annotations

import datetime as dt
import html
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import structlog

from ..constants import IST
from ..models import Order, OrderIntent, Signal

log = structlog.get_logger(__name__)
IST_TZ = ZoneInfo(IST)

_API = "https://api.telegram.org"


class Notifier:
    """No-op base. Used when Telegram is not configured, so callers never need
    to check whether notifications are enabled."""

    enabled = False

    def send(self, text: str, *, silent: bool = False) -> bool:  # noqa: ARG002
        log.info("notify", text=text.replace("\n", " | ")[:300])
        return False

    # --- semantic helpers ------------------------------------------------
    def signal(self, sig: Signal) -> None:
        self.send(f"SIGNAL {sig.action} {sig.plan}\n"
                  f"bar {sig.bar_ts:%Y-%m-%d %H:%M} @ {sig.price:,.2f}\n{sig.reason}")

    def order_placed(self, intent: OrderIntent, order: Order, *, mode: str) -> None:
        tag = "PAPER" if mode == "paper" else "LIVE"
        self.send(f"[{tag}] ORDER {intent.transaction_type.value} "
                  f"{intent.quantity} x {intent.instrument.display_name}\n"
                  f"type {intent.order_type.value}  status {order.status.value}\n"
                  f"id {order.order_id or '-'}  plan {intent.plan}")

    def order_filled(self, order: Order, *, name: str = "") -> None:
        self.send(f"FILLED {name or order.security_id}\n"
                  f"{order.filled_qty} @ {order.avg_price:,.2f}  (id {order.order_id})")

    def guard_blocked(self, guard: str, detail: str, *, plan: str = "") -> None:
        self.send(f"BLOCKED by {guard}\n{plan}: {detail}")

    def halted(self, reason: str) -> None:
        self.send(f"*** TRADING HALTED ***\n{reason}", silent=False)

    def error(self, what: str, detail: str) -> None:
        self.send(f"ERROR {what}\n{detail}")

    def heartbeat(self, summary: str) -> None:
        self.send(f"heartbeat {dt.datetime.now(IST_TZ):%H:%M}\n{summary}", silent=True)


class TelegramNotifier(Notifier):
    enabled = True

    def __init__(self, bot_token: str, chat_id: str, *, timeout: float = 5.0) -> None:
        self._url = f"{_API}/bot{bot_token}/sendMessage"
        self.chat_id = str(chat_id)
        self._client = httpx.Client(timeout=timeout)
        self.failures = 0
        self.sent = 0

    def send(self, text: str, *, silent: bool = False) -> bool:
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": f"<pre>{html.escape(text)}</pre>",
            "parse_mode": "HTML",
            "disable_notification": silent,
        }
        try:
            resp = self._client.post(self._url, json=payload)
            resp.raise_for_status()
        except Exception as exc:
            # Never let a notification failure reach the trading path.
            self.failures += 1
            log.warning("notify.failed", error=str(exc)[:200], failures=self.failures)
            return False
        self.sent += 1
        return True

    def close(self) -> None:
        self._client.close()


def build_notifier(bot_token: str, chat_id: str) -> Notifier:
    """Returns a working notifier, or the no-op one if unconfigured."""
    if bot_token and chat_id:
        return TelegramNotifier(bot_token, chat_id)
    log.warning("notify.disabled",
                hint="set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to get alerts; "
                     "an unattended bot with no alerting fails silently")
    return Notifier()


__all__ = ["Notifier", "TelegramNotifier", "build_notifier"]
