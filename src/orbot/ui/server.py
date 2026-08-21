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



# 1500 two-hour bars is roughly 480 days, so the dashboard was fetching about
# 8,000 hourly candles and resampling them on every poll -- per plan, every 30
# seconds. On a 1 GB instance that filled memory, pushed 1.7 GB into swap and
# left the box thrashing, which is why the chart showed "no data": its own
# requests were timing out. 400 bars is ~50 trading days at 2h, which is more
# chart than anyone reads, and the strategy needs only 114 to warm up.
DEFAULT_CHART_BARS = 400


def _plan_underlyings() -> list[str] | None:
    """Underlyings the enabled plans need, for the instrument filter.

    Calling build_broker() without this parses all 155,535 rows of the scrip
    master -- 180 MB and eleven seconds -- because the filtered cache is keyed
    on the filter, and "no filter" is a different key from the one the engine
    warmed. On a 1 GB instance that allocation goes straight into swap, and it
    happened on every dashboard poll: the chart endpoint simply never returned.
    """
    names = {p.execution.underlying for p in load_plans()
             if p.enabled and p.execution.underlying}
    return sorted(names) or None


def _plans_segment(plans) -> object | None:
    """The exchange the enabled plans trade, when they agree on one.

    Reporting NSE hours by default told the dashboard the market was shut at
    17:00 while the MCX plans it was displaying traded on until 23:30.
    """
    segments = {p.signal.exchange_segment for p in plans if p.enabled}
    return segments.pop() if len(segments) == 1 else None


def build_state() -> dict[str, Any]:
    """Everything the page needs, in one snapshot."""
    settings = get_settings()
    risk = load_risk()
    plans = load_plans()
    calendar = MarketCalendar.load(CONFIG_DIR / "calendar.yaml")
    switch = KillSwitch(HALT_FILE)
    halt = switch.state()
    now = dt.datetime.now(IST_TZ)
    _seg = _plans_segment(plans)

    state: dict[str, Any] = {
        "now": now.isoformat(),
        "broker": settings.broker,
        "mode": settings.trade_mode,
        "is_live": settings.is_live,
        "market": calendar.describe(now, _seg),
        "market_open": calendar.is_open(now, _seg),
        "minutes_to_close": round(calendar.minutes_to_close(now, _seg), 1),
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
            "square_off": (p.risk.square_off_time.strftime("%H:%M")
                           if p.risk.square_off_time else "holds overnight"),
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
            conn, "SELECT COUNT(*) AS n FROM orders WHERE"
                  " substr(created_at,1,10) = ?"
                  " AND status IN ('TRADED','PART_TRADED')", (today,))[0]["n"]

        state["signals"] = _rows(
            conn, "SELECT plan, action, bar_ts, price, source, reason, meta,"
                  " created_at FROM signals ORDER BY id DESC LIMIT 40")
        for s in state["signals"]:
            try:
                s["meta"] = json.loads(s.get("meta") or "{}")
            except ValueError:
                s["meta"] = {}

        # run_id matters on this table. Restarting the engine starts a new run,
        # and in paper mode the positions from earlier runs no longer exist --
        # so without it the panel shows four gold buys for one gold position
        # and there is no way to tell which one you actually hold.
        current_run = _rows(conn, "SELECT MAX(id) AS m FROM runs")[0]["m"]
        state["current_run"] = current_run
        state["orders"] = _rows(
            conn, "SELECT order_id, plan, display_name, security_id, run_id,"
                  " transaction_type, quantity, filled_qty, avg_price, status,"
                  " mode, created_at FROM orders ORDER BY id DESC LIMIT 40")
        for o in state["orders"]:
            o["current_run"] = (o.get("run_id") == current_run)

        state["guards"] = _rows(
            conn, "SELECT guard, allowed, detail, plan, created_at"
                  " FROM guard_events ORDER BY id DESC LIMIT 40")

        # The latest snapshot means the most recent *batch*, not the most
        # recent row per instrument. Grouping per instrument with no time bound
        # kept yesterday's closed positions on screen indefinitely -- gold and
        # silver both showed open all evening from a 23:59 snapshot, while the
        # broker had been flat since the morning.
        state["positions"] = _rows(
            conn, "SELECT security_id, segment, net_qty, avg_price, realised,"
                  " unrealised, taken_at FROM position_snapshots"
                  " WHERE source = 'broker' AND net_qty != 0 AND taken_at ="
                  " (SELECT MAX(taken_at) FROM position_snapshots"
                  "  WHERE source = 'broker')")
    finally:
        conn.close()
    return state


def health() -> dict[str, Any]:
    """Operational vitals. Separate from state because these answer "is the
    machinery alive", not "what did it do" — and it is the first thing you want
    when nothing is happening and you cannot tell whether that is correct."""
    settings = get_settings()
    calendar = MarketCalendar.load(CONFIG_DIR / "calendar.yaml")
    now = dt.datetime.now(IST_TZ)
    _seg = _plans_segment(load_plans())
    out: dict[str, Any] = {
        "market_open": calendar.is_open(now, _seg),
        "market": calendar.describe(now, _seg),
        "minutes_to_close": round(calendar.minutes_to_close(now, _seg), 1),
        "static_ip_proxy": bool(settings.dhan_order_proxy),
        "token": "unknown",
        "token_ok": False,
        "last_run": None,
        "last_signal": None,
        "last_guard": None,
    }
    try:
        if settings.broker == "angel":
            out["token_ok"] = settings.can_self_renew_angel
            out["token"] = ("self-renewing via TOTP" if settings.can_self_renew_angel
                            else "no TOTP — will need a manual login")
        else:
            from ..broker.auth import TokenManager
            tm = TokenManager(settings.dhan_client_id,
                              static_token=settings.dhan_access_token,
                              pin=settings.dhan_pin,
                              totp_secret=settings.dhan_totp_secret,
                              cache_path=settings.token_cache_path)
            out["token_ok"] = tm.can_self_renew
            out["token"] = tm.describe()
    except Exception as exc:
        out["token"] = f"unreadable: {exc}"

    conn = _journal(settings.journal_path)
    if conn is not None:
        try:
            runs = _rows(conn, "SELECT started_at, ended_at, mode FROM runs"
                               " ORDER BY id DESC LIMIT 1")
            if runs:
                out["last_run"] = runs[0]
            sig = _rows(conn, "SELECT bar_ts, action, plan FROM signals"
                              " ORDER BY id DESC LIMIT 1")
            if sig:
                out["last_signal"] = sig[0]
            gd = _rows(conn, "SELECT guard, detail, created_at FROM guard_events"
                             " ORDER BY id DESC LIMIT 1")
            if gd:
                out["last_guard"] = gd[0]
        finally:
            conn.close()
    return out


def chart_payload(plan_name: str, bars: int = DEFAULT_CHART_BARS,
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

    bundle = build_broker(settings, refresh_instruments=False,
                           underlyings=_plan_underlyings())
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



# --- config editing -------------------------------------------------------
# ruamel round-trips YAML with comments, anchors and formatting intact. A
# PyYAML load-and-dump would strip every comment in instruments.yaml and
# collapse the shared &svmkr_params anchor the plans depend on.
def _yaml():
    from ruamel.yaml import YAML
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _config_path():
    return CONFIG_DIR / "instruments.yaml"


def _load_doc():
    path = _config_path()
    with path.open("r", encoding="utf-8") as fh:
        return _yaml().load(fh), path


def _write_doc(doc, path) -> dict[str, Any]:
    """Write, then prove the result still loads. A config that decides what
    trades must never be left broken by a UI action."""
    original = path.read_text(encoding="utf-8")
    backup = path.with_suffix(".yaml.bak")
    backup.write_text(original, encoding="utf-8")
    import io
    buf = io.StringIO()
    _yaml().dump(doc, buf)
    path.write_text(buf.getvalue(), encoding="utf-8")
    try:
        load_plans()
    except Exception as exc:
        path.write_text(original, encoding="utf-8")
        return {"error": f"change rejected, config restored: {exc}"}
    return {"applied": True, "backup": str(backup)}


def _find_plan_node(doc, plan_name):
    for node in doc.get("plans", []):
        if node.get("name") == plan_name:
            return node
    return None


def search_instruments(query: str, limit: int = 25) -> list[dict[str, Any]]:
    """Find something to trade by name.

    Only instruments a signal can actually be computed on are returned —
    indices, cash equity and futures. Options are excluded on purpose: the
    strategy watches an underlying and the resolver picks the contract, so
    offering a specific strike here would invite configuring a plan that
    expires next week.
    """
    from ..broker.factory import build_broker
    from ..constants import InstrumentType

    q = (query or "").strip().upper()
    if len(q) < 2:
        return []
    settings = get_settings()
    registry = build_broker(settings, refresh_instruments=False,
                           underlyings=_plan_underlyings()).registry
    wanted = {InstrumentType.INDEX, InstrumentType.EQUITY,
              InstrumentType.FUTIDX, InstrumentType.FUTSTK}

    out: list[dict[str, Any]] = []
    for inst in registry._by_key.values():
        if inst.instrument_type not in wanted:
            continue
        hay = f"{inst.symbol} {inst.display_name}".upper()
        if q not in hay:
            continue
        out.append({
            "security_id": inst.security_id,
            "segment": inst.segment.value,
            "instrument_type": inst.instrument_type.value,
            "symbol": inst.symbol,
            "display_name": inst.display_name,
            "lot_size": inst.lot_size,
            "expiry": inst.expiry.isoformat() if inst.expiry else None,
            "exact": hay.startswith(q),
        })
        if len(out) > 400:
            break
    out.sort(key=lambda r: (not r["exact"], len(r["symbol"]), r["symbol"]))
    return out[:limit]


PLAN_FIELDS = {
    "timeframe": ("signal", "timeframe"),
    "security_id": ("signal", "security_id"),
    "exchange_segment": ("signal", "exchange_segment"),
    "instrument_type": ("signal", "instrument_type"),
    "mode": ("execution", "mode"),
    "underlying": ("execution", "underlying"),
    "product": ("execution", "product"),
    "order_type": ("execution", "order_type"),
    "qty_lots": ("execution", "qty_lots"),
    "quantity": ("execution", "quantity"),
    "square_off_time": ("risk", "square_off_time"),
    "max_trades_per_day": ("risk", "max_trades_per_day"),
    "sl_pct": ("risk", "sl_pct"),
    "target_pct": ("risk", "target_pct"),
}
LEG_FIELDS = {"option_type", "strike", "expiry"}


def update_plan(plan_name: str, changes: dict[str, Any], *,
                dry_run: bool = True) -> dict[str, Any]:
    """Edit one plan's instrument, timeframe, execution and risk settings."""
    from ..engine.timeframe import Timeframe, TimeframeError

    doc, path = _load_doc()
    node = _find_plan_node(doc, plan_name)
    if node is None:
        return {"error": f"no plan named {plan_name!r}"}

    if "timeframe" in changes:
        try:
            changes["timeframe"] = Timeframe.parse(changes["timeframe"]).label
        except TimeframeError as exc:
            return {"error": str(exc)}

    diff: list[dict[str, str]] = []
    for key, value in changes.items():
        if key in LEG_FIELDS:
            leg = node.setdefault("execution", {}).setdefault("on_buy", {})
            before = leg.get(key)
            if str(before) != str(value):
                diff.append({"key": f"on_buy.{key}", "from": str(before),
                             "to": str(value)})
                if not dry_run:
                    leg[key] = value
            continue
        if key not in PLAN_FIELDS:
            continue
        section, field = PLAN_FIELDS[key]
        block = node.setdefault(section, {})
        before = block.get(field)
        if str(before) == str(value):
            continue
        diff.append({"key": f"{section}.{field}", "from": str(before),
                     "to": str(value)})
        if not dry_run:
            block[field] = value

    if dry_run or not diff:
        return {"diff": diff, "applied": False}
    result = _write_doc(doc, path)
    result["diff"] = diff
    return result


def configured_params(plan_name: str) -> dict[str, Any]:
    """The parameters a plan is actually running, for the playground to show.

    Reads the plan rather than the YAML defaults, so a plan that overrides the
    shared `&svmkr_params` anchor shows its own values. Falls back to the
    chart-legend defaults for anything a plan leaves unset, which is what the
    strategy itself does.
    """
    plan = next((p for p in load_plans() if p.name == plan_name), None)
    if plan is None:
        return {"error": f"no plan named {plan_name!r}"}
    from ..engine.strategy import params_from_config

    params = params_from_config(plan.strategy.params)
    return {
        "plan": plan.name,
        "ut_key": params.ut_key,
        "ut_atr_period": params.ut_atr_period,
        "hma_length": params.hma_length,
        "orb_minutes": params.orb_minutes,
        "orb_session": params.orb_session,
        "use_heikin_ashi": params.use_heikin_ashi,
        "timeframe": plan.signal.timeframe,
    }


def apply_params(changes: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
    """Edit strategy parameters.

    The plans share one `&svmkr_params` anchor, so a change here applies to
    every plan that references it. That is surfaced in the returned payload
    rather than left as a surprise.
    """
    doc, path = _load_doc()
    target = doc.get("defaults")
    shared = target is not None
    if target is None:
        node = doc.get("plans", [{}])[0]
        target = node.get("strategy", {}).get("params", {})

    diff: list[dict[str, str]] = []
    for key, value in changes.items():
        if key not in PARAM_KEYS:
            continue
        before = target.get(key)
        if str(before) == str(value):
            continue
        diff.append({"key": key, "from": str(before), "to": str(value)})
        if not dry_run:
            target[key] = value

    if dry_run or not diff:
        return {"diff": diff, "applied": False, "shared": shared,
                "affects": [p.name for p in load_plans()] if shared else []}
    result = _write_doc(doc, path)
    result["diff"] = diff
    result["shared"] = shared
    return result


def set_plan_enabled(plan_name: str, enabled: bool, *,
                     dry_run: bool = True) -> dict[str, Any]:
    doc, path = _load_doc()
    node = _find_plan_node(doc, plan_name)
    if node is None:
        return {"error": f"no plan named {plan_name!r}"}
    if bool(node.get("enabled")) == enabled:
        return {"changed": False, "enabled": enabled,
                "detail": f"{plan_name} already "
                          f"{'enabled' if enabled else 'disabled'}"}
    if dry_run:
        return {"changed": True, "enabled": enabled, "applied": False}
    node["enabled"] = enabled
    result = _write_doc(doc, path)
    result.update({"changed": True, "enabled": enabled})
    return result

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

    @app.middleware("http")
    async def _no_store(request, call_next):
        """Never let the browser cache this dashboard.

        The page and its script change as the system is developed, and a
        stale copy is worse than a slow one: it silently shows old numbers
        and missing controls, which reads as a bug in the software rather
        than in the cache.
        """
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        return response

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        # Cache-bust the script the same way, so a fresh page never pairs with
        # a stale chart.js.
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        stamp = int((STATIC / "chart.js").stat().st_mtime)
        html = html.replace("/static/chart.js", f"/static/chart.js?v={stamp}")
        return HTMLResponse(html)

    @app.get("/api/state")
    def state() -> dict:
        return build_state()

    @app.get("/api/chart/{plan_name}")
    def chart(plan_name: str, bars: int = DEFAULT_CHART_BARS, ut_key: float | None = None,
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

    @app.get("/api/instruments")
    def instruments(q: str = "", limit: int = 25) -> list:
        return search_instruments(q, limit=limit)

    @app.post("/api/plan/{plan_name}")
    def edit_plan(plan_name: str, payload: dict) -> dict:
        if not allow_control:
            raise HTTPException(403, "controls disabled")
        return update_plan(plan_name, payload.get("changes") or {},
                           dry_run=bool(payload.get("dry_run", True)))

    @app.get("/api/health")
    def health_endpoint() -> dict:
        return health()

    @app.get("/api/performance/{plan_name}")
    def performance(plan_name: str, bars: int = DEFAULT_CHART_BARS,
                    model_options: bool = False, period: str = "all",
                    direction: str = "long") -> dict:
        from .performance import performance_payload
        return performance_payload(plan_name, bars=bars,
                                   model_options=model_options,
                                   period=period, direction=direction)

    @app.post("/api/plan/{plan_name}/enabled")
    def plan_enabled(plan_name: str, payload: dict) -> dict:
        if not allow_control:
            raise HTTPException(403, "controls disabled")
        return set_plan_enabled(plan_name, bool(payload.get("enabled")),
                                dry_run=bool(payload.get("dry_run", True)))

    @app.post("/api/flatten")
    def flatten(payload: dict | None = None) -> dict:
        """Close every open position. Risk-reducing only — there is deliberately
        no order-entry endpoint, because a Buy button in a bot dashboard is the
        fastest route to a position the bot does not know it holds."""
        if not allow_control:
            raise HTTPException(403, "controls disabled")
        if not (payload or {}).get("confirm"):
            raise HTTPException(400, "confirm required")
        import asyncio

        from ..broker.factory import build_broker
        from ..execution.oms import build_oms
        from ..ops.journal import Journal
        from ..ops.notifier import build_notifier
        from ..engine.runner import TradingEngine

        settings = get_settings()
        journal = Journal(settings.journal_path)
        try:
            bundle = build_broker(settings, refresh_instruments=False,
                           underlyings=_plan_underlyings())
            oms = build_oms(settings.trade_mode, bundle.client,
                            state_path=settings.paper_state_path)
            positions = oms.positions()
            if not positions:
                return {"closed": 0, "detail": "nothing open"}
            engine = TradingEngine(
                settings=settings, plans=load_plans(), risk=load_risk(),
                registry=bundle.registry, client=bundle.client, oms=oms,
                journal=journal,
                notifier=build_notifier(settings.telegram_bot_token,
                                        settings.telegram_chat_id),
                killswitch=KillSwitch(HALT_FILE),
                calendar=MarketCalendar.load(CONFIG_DIR / "calendar.yaml"),
                feed_instrument=bundle.feed_instrument)
            engine.book.adopt(positions)
            asyncio.run(engine.flatten("flatten from the dashboard"))
            return {"closed": len(positions)}
        finally:
            journal.close()

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
