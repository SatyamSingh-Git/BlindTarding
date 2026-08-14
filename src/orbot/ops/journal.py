"""SQLite audit trail.

Every signal, order, fill and guard decision lands here. When something goes
wrong at 10:47 on a Tuesday, this is the only record of what the bot believed at
the time — logs get rotated, terminal scrollback disappears, and the broker only
knows about orders that actually reached it, not the ones a guard stopped.

**Deduplication lives here rather than in memory.** A restart clears an in-memory
set, so a bot that crashes and comes back mid-session would happily re-fire a
signal it had already acted on. A `UNIQUE` constraint on the signal's dedupe key
survives restarts, and the insert itself is the check — no read-then-write race.

Written with `synchronous=FULL` on the orders table path: this is small, low-rate
data where losing the last write to a power cut is worse than the microseconds
saved.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import structlog

from ..constants import IST
from ..models import Order, OrderIntent, Position, Signal

log = structlog.get_logger(__name__)
IST_TZ = ZoneInfo(IST)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    mode         TEXT NOT NULL,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER,
    dedupe_key   TEXT NOT NULL UNIQUE,
    plan         TEXT NOT NULL,
    action       TEXT NOT NULL,
    bar_ts       TEXT NOT NULL,
    price        REAL NOT NULL,
    source       TEXT,
    reason       TEXT,
    meta         TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_plan_bar ON signals(plan, bar_ts);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER,
    signal_id       INTEGER,
    correlation_id  TEXT NOT NULL UNIQUE,
    order_id        TEXT,
    plan            TEXT NOT NULL,
    security_id     TEXT NOT NULL,
    segment         TEXT NOT NULL,
    display_name    TEXT,
    transaction_type TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    order_type      TEXT,
    price           REAL,
    status          TEXT,
    filled_qty      INTEGER DEFAULT 0,
    avg_price       REAL DEFAULT 0,
    mode            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    raw             TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_plan ON orders(plan, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS guard_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER,
    plan         TEXT,
    guard        TEXT NOT NULL,
    allowed      INTEGER NOT NULL,
    detail       TEXT,
    signal_key   TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_guard_events_guard ON guard_events(guard, created_at);

CREATE TABLE IF NOT EXISTS position_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER,
    taken_at     TEXT NOT NULL,
    source       TEXT NOT NULL,          -- 'local' or 'broker'
    security_id  TEXT NOT NULL,
    segment      TEXT NOT NULL,
    net_qty      INTEGER NOT NULL,
    avg_price    REAL,
    realised     REAL,
    unrealised   REAL
);

CREATE TABLE IF NOT EXISTS daily_pnl (
    trade_date   TEXT PRIMARY KEY,
    realised     REAL NOT NULL DEFAULT 0,
    unrealised   REAL NOT NULL DEFAULT 0,
    trades       INTEGER NOT NULL DEFAULT 0,
    halted       INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL
);
"""


class DuplicateSignal(Exception):
    """This exact signal has already been recorded. Guard 5 acts on it."""


def _now() -> str:
    return dt.datetime.now(IST_TZ).isoformat()


class Journal:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        # Small, low-rate, irreplaceable data. Durability beats throughput.
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        self.run_id: int | None = None

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- runs ------------------------------------------------------------
    def start_run(self, mode: str, note: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, mode, note) VALUES (?, ?, ?)",
            (_now(), mode, note),
        )
        self.conn.commit()
        self.run_id = int(cur.lastrowid)
        return self.run_id

    def end_run(self) -> None:
        if self.run_id is None:
            return
        self.conn.execute("UPDATE runs SET ended_at = ? WHERE id = ?", (_now(), self.run_id))
        self.conn.commit()

    # --- signals ---------------------------------------------------------
    def record_signal(self, signal: Signal) -> int:
        """Insert a signal. Raises `DuplicateSignal` if already seen.

        The UNIQUE constraint is the dedupe check — asking first and inserting
        second would leave a window where two paths both see "not present".
        """
        try:
            cur = self.conn.execute(
                "INSERT INTO signals (run_id, dedupe_key, plan, action, bar_ts, price,"
                " source, reason, meta, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.run_id, signal.dedupe_key, signal.plan, signal.action,
                 signal.bar_ts.isoformat(), float(signal.price), signal.source,
                 signal.reason, json.dumps(signal.meta, default=str), _now()),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateSignal(signal.dedupe_key) from exc
        self.conn.commit()
        return int(cur.lastrowid)

    def has_signal(self, dedupe_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM signals WHERE dedupe_key = ? LIMIT 1", (dedupe_key,)
        ).fetchone()
        return row is not None

    # --- orders ----------------------------------------------------------
    def record_order(self, intent: OrderIntent, order: Order | None, *,
                     mode: str, signal_id: int | None = None) -> int:
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO orders (run_id, signal_id, correlation_id, order_id, plan,"
            " security_id, segment, display_name, transaction_type, quantity,"
            " order_type, price, status, filled_qty, avg_price, mode,"
            " created_at, updated_at, raw)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(correlation_id) DO UPDATE SET"
            "   order_id=excluded.order_id, status=excluded.status,"
            "   filled_qty=excluded.filled_qty, avg_price=excluded.avg_price,"
            "   updated_at=excluded.updated_at, raw=excluded.raw",
            (self.run_id, signal_id, intent.correlation_id,
             order.order_id if order else None, intent.plan,
             intent.instrument.security_id, intent.instrument.segment.value,
             intent.instrument.display_name, intent.transaction_type.value,
             int(intent.quantity), intent.order_type.value, float(intent.price),
             order.status.value if order else "TRANSIT",
             order.filled_qty if order else 0,
             order.avg_price if order else 0.0,
             mode, now, now,
             json.dumps(order.raw, default=str) if order else None),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_order(self, order: Order) -> None:
        self.conn.execute(
            "UPDATE orders SET order_id = ?, status = ?, filled_qty = ?,"
            " avg_price = ?, updated_at = ?, raw = ? WHERE correlation_id = ?",
            (order.order_id, order.status.value, order.filled_qty, order.avg_price,
             _now(), json.dumps(order.raw, default=str), order.correlation_id),
        )
        self.conn.commit()

    def open_orders(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM orders WHERE status NOT IN"
            " ('TRADED','REJECTED','CANCELLED','CLOSED') ORDER BY created_at"
        ))

    def trades_today(self, plan: str | None = None,
                     *, day: dt.date | None = None) -> int:
        day = day or dt.datetime.now(IST_TZ).date()
        sql = ("SELECT COUNT(*) AS n FROM orders WHERE date(created_at) = ?"
               " AND status IN ('TRADED','PART_TRADED','PENDING','TRANSIT')")
        args: list[Any] = [day.isoformat()]
        if plan:
            sql += " AND plan = ?"
            args.append(plan)
        row = self.conn.execute(sql, args).fetchone()
        return int(row["n"]) if row else 0

    # --- guards ----------------------------------------------------------
    def record_guard(self, guard: str, allowed: bool, detail: str, *,
                     plan: str | None = None, signal_key: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO guard_events (run_id, plan, guard, allowed, detail,"
            " signal_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.run_id, plan, guard, int(allowed), detail, signal_key, _now()),
        )
        self.conn.commit()

    # --- positions and P&L ------------------------------------------------
    def snapshot_positions(self, positions: Iterable[Position], *, source: str) -> None:
        now = _now()
        self.conn.executemany(
            "INSERT INTO position_snapshots (run_id, taken_at, source, security_id,"
            " segment, net_qty, avg_price, realised, unrealised)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(self.run_id, now, source, p.security_id, p.segment.value, p.net_qty,
              p.avg_price, p.realised, p.unrealised) for p in positions],
        )
        self.conn.commit()

    def update_daily_pnl(self, realised: float, unrealised: float, *,
                         trades: int = 0, halted: bool = False,
                         day: dt.date | None = None) -> None:
        day = day or dt.datetime.now(IST_TZ).date()
        self.conn.execute(
            "INSERT INTO daily_pnl (trade_date, realised, unrealised, trades,"
            " halted, updated_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(trade_date) DO UPDATE SET realised=excluded.realised,"
            " unrealised=excluded.unrealised, trades=excluded.trades,"
            " halted=MAX(daily_pnl.halted, excluded.halted),"
            " updated_at=excluded.updated_at",
            (day.isoformat(), realised, unrealised, trades, int(halted), _now()),
        )
        self.conn.commit()

    def daily_pnl(self, day: dt.date | None = None) -> tuple[float, float]:
        day = day or dt.datetime.now(IST_TZ).date()
        row = self.conn.execute(
            "SELECT realised, unrealised FROM daily_pnl WHERE trade_date = ?",
            (day.isoformat(),),
        ).fetchone()
        if row is None:
            return 0.0, 0.0
        return float(row["realised"]), float(row["unrealised"])

    def was_halted_today(self, day: dt.date | None = None) -> bool:
        day = day or dt.datetime.now(IST_TZ).date()
        row = self.conn.execute(
            "SELECT halted FROM daily_pnl WHERE trade_date = ?", (day.isoformat(),)
        ).fetchone()
        return bool(row["halted"]) if row else False


__all__ = ["Journal", "DuplicateSignal"]
