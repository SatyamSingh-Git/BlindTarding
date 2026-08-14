"""ORBot command line.

Phase 0/1 commands — everything here works before a single order is ever placed:

    orbot doctor            check config, credentials and connectivity
    orbot instruments       refresh the scrip master
    orbot resolve NIFTY     look up security ids, expiries, strikes
    orbot chain NIFTY       show the option chain around ATM
    orbot warmup nifty-atm-ce   pull historical candles for a plan
    orbot feed nifty-atm-ce     stream live ticks and print bars as they close
    orbot plans             list configured trade plans
"""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import logging_setup
from .broker.dhan_client import DhanAPIError, DhanClient
from .broker.dhan_feed import DhanFeed, FeedInstrument
from .broker.instruments import (
    InstrumentError,
    InstrumentRegistry,
    download_scrip_master,
    is_fresh,
)
from .config import DATA_DIR, HALT_FILE, TradePlan, get_settings, load_plans, load_risk
from .constants import ExchangeSegment, InstrumentType, OptionType
from .engine.candles import CandleSeries
from .engine.strategy import SvmkrUtHmaOrb, params_from_config
from .models import Action, Candle, Instrument

app = typer.Typer(add_completion=False, help="Automated SVMKR_UT_HMA_ORB execution on Dhan.")
console = Console()


def _registry(*, underlyings: list[str] | None = None) -> InstrumentRegistry:
    settings = get_settings()
    path = settings.scrip_master_path
    if not is_fresh(path):
        console.print("[yellow]scrip master missing or stale — downloading[/yellow]")
        download_scrip_master(path)
    return InstrumentRegistry.load(path, underlyings=underlyings)


def _client() -> DhanClient:
    settings = get_settings()
    missing = settings.missing_credentials()
    if missing:
        console.print(f"[red]missing credentials: {', '.join(missing)}[/red]")
        console.print("Copy .env.example to .env and fill it in.")
        raise typer.Exit(1)
    return DhanClient(
        settings.dhan_client_id,
        settings.dhan_access_token,
        order_proxy=settings.dhan_order_proxy or None,
        proxy_data_calls=settings.proxy_data_calls,
        max_orders_per_second=settings.max_orders_per_second,
        max_orders_per_day=settings.max_orders_per_day,
    )


def _find_plan(name: str) -> TradePlan:
    for plan in load_plans():
        if plan.name == name:
            return plan
    console.print(f"[red]no plan named {name!r} in config/instruments.yaml[/red]")
    raise typer.Exit(1)


def _signal_instrument(plan: TradePlan, registry: InstrumentRegistry) -> Instrument:
    return registry.get(plan.signal.exchange_segment, plan.signal.security_id)


@app.callback()
def _root(log_level: str = typer.Option("INFO", "--log-level")) -> None:
    logging_setup.configure(log_level)


# --- diagnostics --------------------------------------------------------
@app.command()
def doctor() -> None:
    """Check configuration, credentials and connectivity. Read-only — places no
    orders and touches nothing."""
    settings = get_settings()
    risk = load_risk()
    plans = load_plans()
    ok = True

    table = Table(title="orbot doctor", show_lines=False)
    table.add_column("check")
    table.add_column("result")

    def row(name: str, good: bool, detail: str) -> None:
        nonlocal ok
        ok = ok and good
        mark = "[green]OK[/green]" if good else "[red]FAIL[/red]"
        table.add_row(name, f"{mark}  {detail}")

    missing = settings.missing_credentials()
    row("credentials", not missing, "present" if not missing else f"missing {', '.join(missing)}")

    mode_warn = "[red]LIVE — real money[/red]" if settings.is_live else "paper (simulated)"
    row("trade mode", True, mode_warn)

    row("static IP proxy", bool(settings.dhan_order_proxy),
        "configured" if settings.dhan_order_proxy
        else "NOT SET — Dhan requires a whitelisted static IP for order placement")

    row("kill switch", not HALT_FILE.exists(),
        "armed (HALT file present — trading blocked)" if HALT_FILE.exists() else "clear")

    enabled = [p.name for p in plans if p.enabled]
    row("trade plans", True,
        f"{len(plans)} configured, {len(enabled)} enabled"
        + (f" ({', '.join(enabled)})" if enabled else ""))

    row("risk limits", True,
        f"daily loss cap Rs.{risk.max_daily_loss:,.0f}, "
        f"max {risk.max_open_positions} positions, "
        f"feed staleness {risk.feed_staleness_seconds}s")

    sm = settings.scrip_master_path
    row("scrip master", sm.exists(),
        f"{sm.name} ({sm.stat().st_size // (1 << 20)} MB, "
        f"{'fresh' if is_fresh(sm) else 'STALE — run: orbot instruments'})"
        if sm.exists() else "not downloaded — run: orbot instruments")

    if not missing:
        try:
            with _client() as client:
                balance = client.available_balance()
            row("dhan api", True, f"reachable, available balance Rs.{balance:,.2f}")
        except DhanAPIError as exc:
            row("dhan api", False, str(exc)[:160])

    console.print(table)
    if not ok:
        raise typer.Exit(1)


@app.command()
def instruments(force: bool = typer.Option(False, "--force", "-f")) -> None:
    """Download today's scrip master."""
    settings = get_settings()
    path = settings.scrip_master_path
    if is_fresh(path) and not force:
        console.print(f"[green]already fresh[/green] {path} (use --force to re-download)")
        return
    console.print("downloading scrip master (~34 MB)...")
    download_scrip_master(path)
    registry = InstrumentRegistry.load(path)
    console.print(f"[green]done[/green] {len(registry):,} instruments -> {path}")


@app.command()
def plans() -> None:
    """List configured trade plans."""
    configured = load_plans()
    if not configured:
        console.print("[yellow]no plans in config/instruments.yaml[/yellow]")
        return
    table = Table(title="trade plans")
    for col in ("name", "on", "signal", "tf", "execution", "size", "square off"):
        table.add_column(col)
    for plan in configured:
        exec_desc = plan.execution.mode
        if plan.execution.mode == "option" and plan.execution.on_buy:
            leg = plan.execution.on_buy
            exec_desc = (f"{plan.execution.underlying} {leg.strike} "
                         f"{leg.option_type} {leg.expiry}")
        elif plan.execution.mode == "future":
            exec_desc = f"{plan.execution.underlying} future"
        size = (f"{plan.execution.quantity} qty" if plan.execution.quantity
                else f"{plan.execution.qty_lots} lot(s)")
        table.add_row(
            plan.name,
            "[green]yes[/green]" if plan.enabled else "[dim]no[/dim]",
            f"{plan.signal.exchange_segment.value}/{plan.signal.security_id}",
            f"{plan.signal.timeframe}m",
            exec_desc, size, plan.risk.square_off_time.strftime("%H:%M"),
        )
    console.print(table)


# --- instrument lookup --------------------------------------------------
@app.command()
def resolve(
    symbol: str,
    segment: str = typer.Option("IDX_I", "--segment", "-s"),
    instrument_type: str = typer.Option("INDEX", "--type", "-t"),
) -> None:
    """Look up an instrument's security id."""
    registry = _registry()
    try:
        inst = registry.find(symbol, ExchangeSegment(segment), InstrumentType(instrument_type))
    except (InstrumentError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None

    table = Table(title=str(inst))
    table.add_column("field")
    table.add_column("value")
    for label, value in (
        ("security_id", inst.security_id),
        ("segment", inst.segment.value),
        ("instrument_type", inst.instrument_type.value),
        ("symbol", inst.symbol),
        ("lot_size", str(inst.lot_size)),
        ("tick_size", str(inst.tick_size)),
        ("expiry", str(inst.expiry) if inst.expiry else "-"),
        ("series", inst.series or "-"),
    ):
        table.add_row(label, value)
    console.print(table)


@app.command()
def chain(
    underlying: str,
    spot: float = typer.Option(..., "--spot", help="Spot price, to locate ATM"),
    option_type: str = typer.Option("CE", "--type", "-t"),
    expiry_rule: str = typer.Option("nearest", "--expiry", "-e"),
    width: int = typer.Option(3, "--width", "-w", help="Strikes either side of ATM"),
) -> None:
    """Show the option chain around ATM — verifies strike and expiry selection
    before any of it is used to place an order."""
    registry = _registry(underlyings=[underlying])
    opt = OptionType(option_type.upper())
    itype = InstrumentType.OPTIDX
    try:
        expiry = registry.select_expiry(underlying, itype, expiry_rule)
    except InstrumentError:
        itype = InstrumentType.OPTSTK
        expiry = registry.select_expiry(underlying, itype, expiry_rule)

    upcoming = registry.expiries(underlying, itype)[:6]
    console.print(f"expiries: {', '.join(str(e) for e in upcoming)}")
    console.print(f"selected: [cyan]{expiry}[/cyan] (rule: {expiry_rule}), spot {spot}")

    atm = registry.select_option(underlying, opt, spot, expiry,
                                 instrument_type=itype, offset=0)
    table = Table(title=f"{underlying} {opt.value} chain — {expiry}")
    for col in ("offset", "strike", "security_id", "lot", "display name"):
        table.add_column(col)
    for offset in range(width, -width - 1, -1):
        try:
            inst = registry.select_option(underlying, opt, spot, expiry,
                                          instrument_type=itype, offset=offset)
        except InstrumentError:
            continue
        label = "ATM" if offset == 0 else (f"ITM{offset}" if offset > 0 else f"OTM{-offset}")
        style = "[bold cyan]" if inst.security_id == atm.security_id else ""
        close = "[/bold cyan]" if style else ""
        table.add_row(f"{style}{label}{close}", f"{inst.strike:,.2f}",
                      inst.security_id, str(inst.lot_size), inst.display_name)
    console.print(table)


# --- data ---------------------------------------------------------------
@app.command()
def warmup(
    plan_name: str,
    days: int = typer.Option(10, "--days", "-d"),
    show: int = typer.Option(10, "--show", "-n"),
) -> None:
    """Pull historical candles for a plan's signal instrument.

    This is the Phase 1 exit check: the printed bars must match the Dhan chart
    exactly, timestamps included. A silent timezone shift here would corrupt
    every session-based rule downstream.
    """
    plan = _find_plan(plan_name)
    registry = _registry()
    inst = _signal_instrument(plan, registry)
    end = dt.datetime.now()
    start = end - dt.timedelta(days=days)

    with _client() as client:
        candles = client.intraday(inst, plan.signal.timeframe, start, end)

    if not candles:
        console.print("[red]no candles returned[/red] — check the instrument and date range")
        raise typer.Exit(1)

    series = CandleSeries(plan.signal.timeframe)
    series.seed(candles)

    console.print(f"[green]{len(candles)}[/green] x {plan.signal.timeframe}m candles "
                  f"for {inst}")
    console.print(f"range: {candles[0].ts} -> {candles[-1].ts}")
    _print_candles(candles[-show:], title=f"last {show} bars — compare against the Dhan chart")


@app.command()
def feed(
    plan_name: str,
    seconds: int = typer.Option(120, "--seconds", "-s"),
) -> None:
    """Stream live ticks for a plan and print bars as they close."""
    plan = _find_plan(plan_name)
    registry = _registry()
    inst = _signal_instrument(plan, registry)
    settings = get_settings()
    if settings.missing_credentials():
        console.print("[red]credentials required[/red] — see .env.example")
        raise typer.Exit(1)

    series = CandleSeries(plan.signal.timeframe)
    stats = {"ticks": 0, "last": None}

    def on_tick(tick) -> None:
        stats["ticks"] += 1
        stats["last"] = tick
        closed = series.on_tick(tick)
        if closed is not None:
            console.print(f"[cyan]bar close[/cyan] {closed.ts:%H:%M}  "
                          f"O {closed.open} H {closed.high} L {closed.low} "
                          f"C {closed.close} V {closed.volume}")

    async def main() -> None:
        stream = DhanFeed(settings.dhan_client_id, settings.dhan_access_token,
                          on_tick=on_tick)
        stream.add([FeedInstrument(inst.segment, inst.security_id)])
        console.print(f"streaming {inst} for {seconds}s — Ctrl-C to stop")
        task = asyncio.create_task(stream.run())
        ticker = asyncio.create_task(_tick_clock(series, seconds))
        try:
            await asyncio.wait_for(asyncio.shield(ticker), timeout=seconds + 5)
        except (asyncio.TimeoutError, KeyboardInterrupt):
            pass
        finally:
            await stream.stop()
            task.cancel()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

    console.print(f"\nreceived [green]{stats['ticks']}[/green] ticks")
    if stats["last"] is not None:
        last = stats["last"]
        console.print(f"last: ltp {last.ltp} at {last.ts:%Y-%m-%d %H:%M:%S} "
                      f"(volume {last.volume})")
    if series.current is not None:
        c = series.current
        console.print(f"forming bar {c.ts:%H:%M}: O {c.open} H {c.high} "
                      f"L {c.low} C {c.close} V {c.volume}")
    if stats["ticks"] == 0:
        console.print("[yellow]no ticks — market closed, or the instrument is not "
                      "trading right now[/yellow]")


async def _tick_clock(series: CandleSeries, seconds: int) -> None:
    """Close bars on the clock so signals don't wait for the next trade."""
    for _ in range(seconds):
        await asyncio.sleep(1)
        series.flush()


def _print_candles(candles: list[Candle], *, title: str) -> None:
    table = Table(title=title)
    for col in ("timestamp (IST)", "open", "high", "low", "close", "volume"):
        table.add_column(col)
    for c in candles:
        table.add_row(c.ts.strftime("%Y-%m-%d %H:%M"), f"{c.open:,.2f}", f"{c.high:,.2f}",
                      f"{c.low:,.2f}", f"{c.close:,.2f}", f"{c.volume:,}")
    console.print(table)


@app.command()
def signals(
    plan_name: str,
    days: int = typer.Option(30, "--days", "-d"),
    show: int = typer.Option(40, "--show", "-n", help="0 for all"),
    csv_out: Optional[str] = typer.Option(None, "--csv", help="Write signals to a CSV"),
) -> None:
    """Replay history through the strategy and list every signal it produces.

    This is the validation-gate tool (PLAN.md section 6). Compare the timestamps
    printed here against the Buy/Sell labels on your TradingView chart. The gate
    is 200+ signals matching on both direction and bar timestamp, at 100%.
    A near-match is a failure — one bar of drift is one bar of drift on every
    trade.
    """
    plan = _find_plan(plan_name)
    registry = _registry()
    inst = _signal_instrument(plan, registry)
    params = params_from_config(plan.strategy.params)

    end = dt.datetime.now()
    start = end - dt.timedelta(days=days)
    with _client() as client:
        candles = client.intraday(inst, plan.signal.timeframe, start, end)
    if not candles:
        console.print("[red]no candles returned[/red]")
        raise typer.Exit(1)

    strategy = SvmkrUtHmaOrb(params, plan=plan.name)
    result = strategy.evaluate(candles)
    found = strategy.signals(candles)

    console.print(f"[green]{len(candles)}[/green] x {plan.signal.timeframe}m bars "
                  f"for {inst}")
    console.print(f"range: {candles[0].ts:%Y-%m-%d %H:%M} -> {candles[-1].ts:%Y-%m-%d %H:%M}")
    console.print(f"params: ut_key={params.ut_key} atr={params.ut_atr_period} "
                  f"hma={params.hma_length} orb={params.orb_session} "
                  f"ha={params.use_heikin_ashi}")

    warm = params.min_bars
    if len(candles) <= warm:
        console.print(f"[yellow]only {len(candles)} bars — the first {warm} are warmup, "
                      "so signals here are not trustworthy[/yellow]")

    buys = sum(1 for s in found if s.action == Action.BUY)
    sells = len(found) - buys
    sessions = len({c.ts.date() for c in candles})
    console.print(f"\n[bold]{len(found)} signals[/bold] — {buys} buy, {sells} sell, "
                  f"over {sessions} sessions "
                  f"({len(found) / max(sessions, 1):.1f} per session)")
    if params.require_hma_agreement or params.require_orb_breakout:
        raw = sum(result.raw_buy) + sum(result.raw_sell)
        console.print(f"[yellow]filters active — {raw} raw signals reduced to "
                      f"{len(found)}[/yellow]")

    rows = found if show == 0 else found[-show:]
    table = Table(title=f"signals (showing {len(rows)} of {len(found)})")
    for col in ("bar timestamp (IST)", "action", "price", "ut stop", "atr",
                "hma", "orb high", "orb low"):
        table.add_column(col)
    for sig in rows:
        meta = sig.meta
        colour = "green" if sig.action == Action.BUY else "red"
        table.add_row(
            sig.bar_ts.strftime("%Y-%m-%d %H:%M"),
            f"[{colour}]{sig.action}[/{colour}]",
            f"{sig.price:,.2f}",
            _fmt(meta.get("stop")), _fmt(meta.get("atr")),
            _fmt(meta.get("hma")), _fmt(meta.get("orb_high")), _fmt(meta.get("orb_low")),
        )
    console.print(table)

    if csv_out:
        import csv as _csv
        path = Path(csv_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = _csv.writer(fh)
            writer.writerow(["bar_ts", "action", "price", "ut_stop", "atr", "hma",
                             "orb_high", "orb_low"])
            for sig in found:
                m = sig.meta
                writer.writerow([sig.bar_ts.isoformat(), sig.action, sig.price,
                                 m.get("stop"), m.get("atr"), m.get("hma"),
                                 m.get("orb_high"), m.get("orb_low")])
        console.print(f"[green]wrote[/green] {len(found)} signals -> {path}")

    console.print("\n[dim]Compare these timestamps against the Buy/Sell labels on "
                  "your TradingView chart. Any mismatch must be resolved before "
                  "going live.[/dim]")


def _fmt(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return f"{value:,.2f}"
    return str(value)


@app.command()
def halt(clear: bool = typer.Option(False, "--clear", help="Disarm the kill switch")) -> None:
    """Arm or clear the kill switch."""
    if clear:
        HALT_FILE.unlink(missing_ok=True)
        console.print("[green]kill switch cleared[/green] — trading permitted")
    else:
        HALT_FILE.write_text(f"halted at {dt.datetime.now().isoformat()}\n", encoding="utf-8")
        console.print(f"[red]KILL SWITCH ARMED[/red] — {HALT_FILE}")


if __name__ == "__main__":
    app()
