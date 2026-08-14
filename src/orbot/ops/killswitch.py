"""Kill switch.

Two ways to stop the bot, and the difference matters under stress:

* **A file on disk** (`HALT`). Works when the process is wedged, works from
  another terminal, works from a phone over SSH, and survives a restart — a
  crash-loop cannot clear it. This is the one that works when everything else
  has gone wrong.
* **In-process**, set by a guard tripping or by a Telegram command.

The file is checked on every signal rather than cached, because the whole point
is that a human can stop the bot *right now* without needing it to cooperate.
A stat call per signal is free at these rates.

Halting is deliberately sticky within a session: once tripped, it stays tripped
until a human clears it. A limit that un-trips itself when P&L bounces back is
not a limit.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog

from ..constants import IST

log = structlog.get_logger(__name__)
IST_TZ = ZoneInfo(IST)


@dataclass(slots=True)
class HaltState:
    halted: bool
    reason: str
    since: dt.datetime | None = None
    source: str = ""


class KillSwitch:
    def __init__(self, halt_file: Path) -> None:
        self.halt_file = halt_file
        self._reason = ""
        self._since: dt.datetime | None = None

    # --- state -----------------------------------------------------------
    def state(self) -> HaltState:
        if self._reason:
            return HaltState(True, self._reason, self._since, "in-process")
        if self.halt_file.exists():
            try:
                detail = self.halt_file.read_text(encoding="utf-8").strip()
            except OSError:
                detail = ""
            return HaltState(True, detail or "HALT file present", None, "file")
        return HaltState(False, "")

    @property
    def is_halted(self) -> bool:
        return self.state().halted

    # --- transitions -----------------------------------------------------
    def trip(self, reason: str, *, persist: bool = True) -> HaltState:
        """Halt trading. `persist` also writes the file so a restart stays halted.

        A daily-loss breach must survive a restart — otherwise a crash-loop
        becomes an unlimited-loss loop.
        """
        if not self._reason:
            self._reason = reason
            self._since = dt.datetime.now(IST_TZ)
            log.error("killswitch.tripped", reason=reason, persist=persist)
        if persist:
            try:
                self.halt_file.write_text(
                    f"{dt.datetime.now(IST_TZ).isoformat()}  {reason}\n", encoding="utf-8"
                )
            except OSError as exc:
                log.error("killswitch.write_failed", error=str(exc))
        return self.state()

    def clear(self) -> None:
        """Only ever called by an explicit human action."""
        self._reason = ""
        self._since = None
        self.halt_file.unlink(missing_ok=True)
        log.warning("killswitch.cleared")


__all__ = ["KillSwitch", "HaltState"]
