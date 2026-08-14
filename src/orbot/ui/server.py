"""Local dashboard.

Runs on your machine, alongside the bot. It reads the same SQLite journal the
engine writes, so it shows what the bot actually did rather than a second
opinion assembled from somewhere else.

Deliberately **local only** — bound to 127.0.0.1 by default. This page exposes
positions, P&L and a kill switch; none of that should be reachable from a
network. The bind address is configurable for the case where you run the bot on
a VPS and tunnel in, but the default assumes you do not want to think about it.

Read-mostly by design. The only state-changing endpoints are the kill switch and
flatten, both of which require an explicit POST — nothing here can accidentally
place an order.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..config import CONFIG_DIR, HALT_FILE, get_settings, load_plans, load_risk
from ..constants import IST
from ..ops.killswitch import KillSwitch
from ..ops.market_calendar import MarketCalendar

IST_TZ = ZoneInfo(IST)
STATIC = Path(__file__).parent / "static"
NEWLINE = chr(10)


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, args)]


def _journal(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    # read-only: the dashboard must never be able to corrupt the audit trail,
    # even by accident, and the engine may be writing concurrently.
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)


def build_state() -> dict[str, Any]:
    """Everything the page needs, in one snapshot."""
    settings = get_settings()
    risk = load_risk()
    plans = load_plans()
    calendar = MarketCalendar.load(CONFIG_DIR / "calendar.yaml")
    switch = KillSwitch(HALT_FILE)
    halt = switch.state()
    now = dt.datetime.now(IST_TZ)

    state: dict[str, Any] = {
        "now": now.isoformat(),
        "broker": settings.broker,
        "mode": settings.trade_mode,
        "is_live": settings.is_live,
        "market": calendar.describe(now),
        "market_open": calendar.is_open(now),
        "minutes_to_close": round(calendar.minutes_to_close(now), 1),
        "halted": halt.halted,
        "halt_reason": halt.reason,
        "halt_source": halt.source,
        "risk": {
            "max_daily_loss": risk.max_daily_loss,
            "max_open_positions": risk.max_open_positions,
            "feed_staleness_seconds": risk.feed_staleness_seconds,
        },
        "plans": [{
            "name": p.name, "enabled": p.enabled,
            "timeframe": p.signal.timeframe,
            "security_id": p.signal.security_id,
            "segment": p.signal.exchange_segment.value,
            "mode": p.execution.mode,
            "underlying": p.execution.underlying,
            "square_off": p.risk.square_off_time.strftime("%H:%M"),
            "max_trades": p.risk.max_trades_per_day,
        } for p in plans],
        "realised": 0.0, "unrealised": 0.0, "trades_today": 0,
        "signals": [], "orders": [], "guards": [], "positions": [],
        "journal": False,
    }

    conn = _journal(settings.journal_path)
    if conn is None:
        return state
    state["journal"] = True
    today = now.date().isoformat()
    try:
        pnl = _rows(conn, "SELECT realised, unrealised FROM daily_pnl "
                          "WHERE trade_date = ?", (today,))
        if pnl:
            state["realised"] = float(pnl[0]["realised"])
            state["unrealised"] = float(pnl[0]["unrealised"])

        state["trades_today"] = _rows(
            conn, "SELECT COUNT(*) AS n FROM orders WHERE date(created_at) = ?"
                  " AND status IN ('TRADED','PART_TRADED')", (today,))[0]["n"]

        state["signals"] = _rows(
            conn, "SELECT plan, action, bar_ts, price, source, reason, meta,"
                  " created_at FROM signals ORDER BY id DESC LIMIT 40")
        for s in state["signals"]:
            try:
                s["meta"] = json.loads(s.get("meta") or "{}")
            except ValueError:
                s["meta"] = {}

        state["orders"] = _rows(
            conn, "SELECT order_id, plan, display_name, security_id,"
                  " transaction_type, quantity, filled_qty, avg_price, status,"
                  " mode, created_at FROM orders ORDER BY id DESC LIMIT 40")

        state["guards"] = _rows(
            conn, "SELECT guard, allowed, detail, plan, created_at"
                  " FROM guard_events ORDER BY id DESC LIMIT 40")

        # Latest broker snapshot per instrument, so the page shows what the
        # broker last confirmed rather than what we hoped.
        state["positions"] = _rows(
            conn, "SELECT security_id, segment, net_qty, avg_price, realised,"
                  " unrealised, MAX(taken_at) AS taken_at FROM position_snapshots"
                  " WHERE source = 'broker' GROUP BY security_id, segment"
                  " HAVING net_qty != 0")
    finally:
        conn.close()
    return state


def chart_payload(plan_name: str, bars: int = 1500,
                  overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Candles plus the indicator overlays, so the chart shows exactly what the
    engine sees — the point being that you can put it beside TradingView.

    Returns a deep window on purpose. Zooming and panning are then pure client
    state with no round trip, which is the difference between a chart that feels
    like a tool and one that feels like a report.

    `overrides` recomputes with different strategy parameters without touching
    config — the parameter playground.
    """
    from ..broker.factory import build_broker
    from ..engine.history import fetch_candles
    from ..engine.strategy import SvmkrUtHmaOrb, params_from_config
    from ..engine.timeframe import Timeframe

    settings = get_settings()
    plan = next((p for p in load_plans() if p.name == plan_name), None)
    if plan is None:
        return {"error": f"no plan named {plan_name!r}"}

    bundle = build_broker(settings, refresh_instruments=False)
    inst = bundle.registry.get(plan.signal.exchange_segment, plan.signal.security_id)
    overrides = dict(overrides or {})
    tf_spec = overrides.pop("timeframe", None)
    tf = Timeframe.parse(tf_spec or plan.signal.timeframe)
    params = params_from_config(plan.strategy.params)

    end = dt.datetime.now()
    span = max(6, int(bars / max(tf.approx_bars_per_session, 0.1)) + 4)
    candles = fetch_candles(bundle.client, inst, tf,
                            end - dt.timedelta(days=int(span * 1.6) + 10), end)
    if not candles:
        return {"error": "no candles returned"}

    if overrides:
        params = params_from_config({**plan.strategy.params, **overrides})

    result = SvmkrUtHmaOrb(params, plan=plan.name).evaluate(candles)
    n = len(result.timestamps)
    start = max(0, n - bars)

    # Slots the exchange should have printed but the broker did not send. Drawn
    # on the chart because a gap silently moves the ATR chain, and a divergence
    # you cannot see is one you will blame on the port.
    gaps: list[str] = []
    if tf.is_intraday:
        by_day: dict[Any, list] = {}
        for ts in result.timestamps[start:]:
            by_day.setdefault(ts.date(), []).append(ts.time())
        step = dt.timedelta(minutes=tf.minutes)
        for day, times in by_day.items():
            # Interior gaps only: walk from the first bar actually present to
            # the last. Starting at 09:15 would flag the whole morning as
            # missing whenever the window happens to begin mid-session.
            have = set(times)
            first, last = min(times), max(times)
            cur = dt.datetime.combine(day, first)
            while cur.time() < last:
                cur += step
                if cur.time() < last and cur.time() not in have:
                    gaps.append(cur.replace(tzinfo=IST_TZ).isoformat())

    return {
        "plan": plan.name,
        "instrument": inst.display_name,
        "timeframe": tf.label,
        "native": tf.is_native,
        "params": {
            "ut_key": params.ut_key, "ut_atr_period": params.ut_atr_period,
            "hma_length": params.hma_length, "orb_session": params.orb_session,
            "use_heikin_ashi": params.use_heikin_ashi,
        },
        "gaps": gaps,
        "signal_count": sum(1 for i in range(start, n)
                            if result.buy[i] or result.sell[i]),
        "sessions": len({t.date() for t in result.timestamps[start:]}),
        "bars": [{
            "t": result.timestamps[i].isoformat(),
            "o": candles[i].open, "h": candles[i].high,
            "l": candles[i].low, "c": candles[i].close,
            "stop": result.ut.stop[i], "hma": result.hma[i],
            "orb_high": result.orb.high[i], "orb_low": result.orb.low[i],
            "atr": result.ut.atr[i],
            "buy": bool(result.buy[i]), "sell": bool(result.sell[i]),
        } for i in range(start, n)],
    }


PARAM_KEYS = ("ut_key", "ut_atr_period", "hma_length", "orb_session",
              "use_heikin_ashi", "require_hma_agreement", "require_orb_breakout")


def configured_params(plan_name: str) -> dict[str, Any]:
    from ..engine.strategy import params_from_config
    plan = next((p for p in load_plans() if p.name == plan_name), None)
    if plan is None:
        return {}
    p = params_from_config(plan.strategy.params)
    return {
        "ut_key": p.ut_key, "ut_atr_period": p.ut_atr_period,
        "hma_length": p.hma_length, "orb_session": p.orb_session,
        "use_heikin_ashi": p.use_heikin_ashi,
        "timeframe": plan.signal.timeframe,
    }


def apply_params(changes: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
    """Edit the strategy parameters in `config/instruments.yaml`.

    Rewritten as targeted line edits rather than a YAML load-and-dump, because
    a round trip through PyYAML would discard every comment in the file and
    collapse the shared anchor the plans depend on.

    Only the numeric and string values on known parameter lines are touched.
    A `.bak` is written first, and if the result no longer parses the original
    is restored — a config that decides what trades should not be left broken
    by a slider.
    """
    path = CONFIG_DIR / "instruments.yaml"
    if not path.exists():
        return {"error": "config/instruments.yaml not found"}

    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    diff: list[dict[str, str]] = []
    out: list[str] = []

    for raw in lines:
        replaced = raw
        stripped = raw.split("#", 1)[0]
        for key, value in changes.items():
            if key not in PARAM_KEYS:
                continue
            marker = f"{key}:"
            if stripped.strip().startswith(marker):
                indent = raw[: len(raw) - len(raw.lstrip())]
                comment = ""
                if "#" in raw:
                    tail = raw.split("#", 1)[1].strip()
                    comment = "  # " + tail.lstrip("# ")
                rendered = ('"%s"' % value if isinstance(value, str)
                            else ("true" if value is True
                                  else "false" if value is False else value))
                new = f"{indent}{key}: {rendered}{comment}" + NEWLINE
                old_val = stripped.split(":", 1)[1].strip()
                if str(old_val) != str(rendered):
                    diff.append({"key": key, "from": old_val, "to": str(rendered)})
                    replaced = new
                break
        out.append(replaced)

    if dry_run or not diff:
        return {"diff": diff, "applied": False, "path": str(path)}

    backup = path.with_suffix(".yaml.bak")
    backup.write_text(original, encoding="utf-8")
    path.write_text("".join(out), encoding="utf-8")
    try:
        load_plans()
    except Exception as exc:
        path.write_text(original, encoding="utf-8")
        return {"error": f"change rejected, config restored: {exc}", "diff": diff,
                "applied": False}
    return {"diff": diff, "applied": True, "backup": str(backup)}


def create_app(*, allow_control: bool = True):
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import HTMLResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:  # pragma: no cover - dependency guidance
        raise SystemExit(
            "the dashboard needs FastAPI — install with:\n"
            '    pip install -e ".[webhook]"'
        ) from exc

    app = FastAPI(title="ORBot", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/api/state")
    def state() -> dict:
        return build_state()

    @app.get("/api/chart/{plan_name}")
    def chart(plan_name: str, bars: int = 1500, ut_key: float | None = None,
              ut_atr_period: int | None = None, hma_length: int | None = None,
              orb_session: str | None = None, timeframe: str | None = None,
              use_heikin_ashi: bool | None = None) -> dict:
        overrides = {k: v for k, v in {
            "ut_key": ut_key, "ut_atr_period": ut_atr_period,
            "hma_length": hma_length, "orb_session": orb_session,
            "use_heikin_ashi": use_heikin_ashi, "timeframe": timeframe,
        }.items() if v is not None}
        return chart_payload(plan_name, bars=bars, overrides=overrides or None)

    @app.get("/api/params/{plan_name}")
    def params(plan_name: str) -> dict:
        return configured_params(plan_name)

    @app.post("/api/params")
    def set_params(payload: dict) -> dict:
        if not allow_control:
            raise HTTPException(403, "controls disabled")
        return apply_params(payload.get("params") or {},
                            dry_run=bool(payload.get("dry_run", True)))

    @app.post("/api/halt")
    def halt() -> dict:
        if not allow_control:
            raise HTTPException(403, "controls disabled")
        KillSwitch(HALT_FILE).trip("armed from the dashboard")
        return {"halted": True}

    @app.post("/api/resume")
    def resume() -> dict:
        if not allow_control:
            raise HTTPException(403, "controls disabled")
        KillSwitch(HALT_FILE).clear()
        return {"halted": False}

    return app


__all__ = ["create_app", "build_state", "chart_payload"]
