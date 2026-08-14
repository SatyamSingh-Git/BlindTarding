"""structlog configuration.

Console output while you watch it, JSON to disk so an incident can be
reconstructed afterwards. Access tokens are scrubbed from every event — the feed
URL carries the token as a query parameter and would otherwise land in the logs.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any

import structlog

_TOKEN_PATTERN = re.compile(r"(token=)[^&\s\"']+", re.IGNORECASE)
_SECRET_KEYS = {"access_token", "dhan_access_token", "token", "telegram_bot_token",
                "dhan_order_proxy", "password"}


def _redact(_logger: Any, _method: str, event: dict[str, Any]) -> dict[str, Any]:
    for key, value in list(event.items()):
        if key in _SECRET_KEYS and value:
            event[key] = "***"
        elif isinstance(value, str) and "token=" in value:
            event[key] = _TOKEN_PATTERN.sub(r"\1***", value)
    return event


def configure(level: str = "INFO", log_dir: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_dir / "orbot.log", encoding="utf-8"))

    logging.basicConfig(format="%(message)s", handlers=handlers,
                        level=getattr(logging, level.upper(), logging.INFO))

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=False),
            _redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty()),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


__all__ = ["configure"]
