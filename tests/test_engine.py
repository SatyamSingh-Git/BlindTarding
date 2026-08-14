"""End-to-end engine tests, in paper mode.

Unit tests prove each piece works. These prove the pieces fit: a signal goes in,
guards run, the resolver picks a contract, the OMS fills it, the position book
updates, and the journal records all of it.

Paper mode shares every line of this path with live except the OMS itself, so
what passes here is what will run with real money.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from orbot.broker.instruments import InstrumentRegistry
from orbot.config import (
    ExecutionConfig,
    GlobalRisk,
    LegConfig,
    PlanRisk,
    Settings,
    SignalConfig,
    StrategyConfig,
    TradePlan,
)
from orbot.constants import ExchangeSegment
from orbot.engine.runner import TradingEngine
from orbot.execution.oms import PaperOMS
from orbot.models import Action, Signal, Tick
from orbot.ops.journal import Journal
from orbot.ops.killswitch import KillSwitch
from orbot.ops.market_calendar import MarketCalendar
from orbot.ops.notifier import Notifier

IST = ZoneInfo("Asia/Kolkata")
NOW = dt.datetime(2026, 8, 14, 11, 0, tzinfo=IST)      # Friday, mid-session

HEADER = (
    "EXCH_ID,SEGMENT,SECURITY_ID,ISIN,INSTRUMENT,UNDERLYING_SECURITY_ID,UNDERLYING_SYMBOL,"
    "SYMBOL_NAME,DISPLAY_NAME,INSTRUMENT_TYPE,SERIES,LOT_SIZE,SM_EXPIRY_DATE,STRIKE_PRICE,"
    "OPTION_TYPE,TICK_SIZE,SM_FREEZE_QTY,"
)


def row(exch, seg, secid, instr, und, symbol, display, lot, expiry, strike, opt,
        tick="0.05", freeze="1800") -> str:
    return (f"{exch},{seg},{secid},NA,{instr},0,{und},{symbol},{display},{instr},"
            f"NA,{lot},{expiry},{strike},{opt},{tick},{freeze},")


class CapturingNotifier(Notifier):
    """Records instead of sending, so tests can assert what a human would see."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str, *, silent: bool = False) -> bool:
        self.messages.append(text)
        return True

    def saw(self, fragment: str) -> bool:
        return any(fragment.lower() in m.lower() for m in self.messages)


@pytest.fixture
def registry(tmp_path: Path) -> InstrumentRegistry:
    lines = [HEADER,
             row("NSE", "I", "13", "INDEX", "NIFTY", "NIFTY", "Nifty 50", "1",
                 "0001-01-01", "-0.01", "XX", freeze="0")]
    secid = 80000
    for strike in range(24_000, 25_001, 100):
        for opt in ("CE", "PE"):
            lines.append(row("NSE", "D", str(secid), "OPTIDX", "NIFTY", "NIFTY",
                             f"NIFTY 20 AUG {strike} {opt}", "65",
                             (dt.date.today() + dt.timedelta(days=6)).isoformat(),
                             f"{strike}.00000", opt, tick="5.0"))
            secid += 1
    path = tmp_path / "s.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return InstrumentRegistry.load(path)


def make_plan(**overrides) -> TradePlan:
    base = dict(
        name="nifty-atm-ce",
        enabled=True,
        signal=SignalConfig(exchange_segment=ExchangeSegment.IDX_I,
                            security_id="13", timeframe="5m"),
        strategy=StrategyConfig(),
        execution=ExecutionConfig(
            mode="option", underlying="NIFTY",
            on_buy=LegConfig(option_type="CE", strike="ATM", expiry="nearest"),
            qty_lots=1),
        risk=PlanRisk(max_trades_per_day=4, square_off_time=dt.time(15, 10)),
    )
    base.update(overrides)
    return TradePlan(**base)


@pytest.fixture
def engine(tmp_path, registry):
    journal = Journal(tmp_path / "j.db")
    journal.start_run("paper")
    notifier = CapturingNotifier()
    eng = TradingEngine(
        settings=Settings(dhan_client_id="", dhan_access_token="", trade_mode="paper"),
        plans=[make_plan()],
        risk=GlobalRisk(max_daily_loss=5000.0, max_open_positions=3,
                        feed_staleness_seconds=60, signal_staleness_seconds=120),
        registry=registry,
        client=None,
        oms=PaperOMS(starting_funds=500_000.0, slippage_pct=0.0),
        journal=journal,
        notifier=notifier,
        killswitch=KillSwitch(tmp_path / "HALT"),
        calendar=MarketCalendar(),
    )
    # Pretend the feed is alive and NIFTY is at 24,287.
    state = eng.states["nifty-atm-ce"]
    eng.last_prices[state.instrument.key] = 24_287.0
    yield eng, notifier, journal
    journal.close()


def buy(minute: int = 0) -> Signal:
    return Signal(plan="nifty-atm-ce", action=Action.BUY,
                  bar_ts=NOW - dt.timedelta(seconds=30), price=24_287.0)


def sell() -> Signal:
    return Signal(plan="nifty-atm-ce", action=Action.SELL,
                  bar_ts=NOW - dt.timedelta(seconds=20), price=24_310.0)


async def feed_context(eng, sig, *, tick_age: float = 2.0):
    """Build a context the way the engine does, but with a controlled clock."""
    state = eng.states["nifty-atm-ce"]
    ctx = await eng._context(state, sig)
    ctx.now = NOW
    ctx.last_tick_age = tick_age
    return ctx


class TestValidation:
    def test_a_good_plan_validates(self, engine):
        eng, _, _ = engine
        assert eng.validate() == []

    def test_no_enabled_plans_is_a_problem(self, tmp_path, registry):
        journal = Journal(tmp_path / "j.db")
        try:
            eng = TradingEngine(
                settings=Settings(), plans=[make_plan(enabled=False)],
                risk=GlobalRisk(), registry=registry, client=None,
                oms=PaperOMS(), journal=journal, notifier=Notifier(),
                killswitch=KillSwitch(tmp_path / "HALT"), calendar=MarketCalendar())
            assert eng.validate()
        finally:
            journal.close()

    def test_a_daily_timeframe_is_rejected_for_live_trading(self, tmp_path, registry):
        """Daily bars close once a day — there is nothing to stream."""
        journal = Journal(tmp_path / "j.db")
        try:
            plan = make_plan(signal=SignalConfig(
                exchange_segment=ExchangeSegment.IDX_I, security_id="13",
                timeframe="1D"))
            with pytest.raises(ValueError, match="intraday"):
                TradingEngine(
                    settings=Settings(), plans=[plan], risk=GlobalRisk(),
                    registry=registry, client=None, oms=PaperOMS(),
                    journal=journal, notifier=Notifier(),
                    killswitch=KillSwitch(tmp_path / "HALT"),
                    calendar=MarketCalendar())
        finally:
            journal.close()


class TestEntryAndExit:
    def test_a_buy_opens_a_position_end_to_end(self, engine):
        eng, notifier, journal = engine
        state = eng.states["nifty-atm-ce"]
        sig = buy()

        async def go():
            ctx = await feed_context(eng, sig)
            eng.guards.check_signal(ctx)          # warm the path
            journal.record_signal(sig)
            await eng._enter(state, sig, ctx)

        asyncio.run(go())

        positions = eng.book.open_positions()
        assert len(positions) == 1
        assert positions[0].net_qty == 65          # 1 lot
        assert state.position_key is not None
        assert notifier.saw("ORDER BUY")

    def test_the_contract_chosen_is_the_atm_call(self, engine):
        eng, _, journal = engine
        state = eng.states["nifty-atm-ce"]
        sig = buy()

        async def go():
            ctx = await feed_context(eng, sig)
            await eng._enter(state, sig, ctx)

        asyncio.run(go())
        row = list(journal.conn.execute("SELECT * FROM orders"))[0]
        assert "24300" in row["display_name"]
        assert "CE" in row["display_name"]
        assert row["quantity"] == 65

    def test_a_sell_closes_the_position(self, engine):
        eng, notifier, journal = engine
        state = eng.states["nifty-atm-ce"]

        async def go():
            entry = buy()
            await eng._enter(state, entry, await feed_context(eng, entry))
            # Mark the option so the exit has a reference price.
            key = state.position_key
            eng.last_prices[key] = 150.0
            exit_sig = sell()
            await eng._exit(state, exit_sig, await feed_context(eng, exit_sig))

        asyncio.run(go())
        assert eng.book.open_positions() == []
        assert state.position_key is None

    def test_a_buy_while_already_long_is_ignored(self, engine):
        """The signals alternate, but a duplicate must not double the size."""
        eng, _, journal = engine
        state = eng.states["nifty-atm-ce"]

        async def go():
            for _ in range(3):
                sig = buy()
                await eng._enter(state, sig, await feed_context(eng, sig))

        asyncio.run(go())
        assert eng.book.open_positions()[0].net_qty == 65

    def test_a_sell_with_no_position_does_nothing(self, engine):
        eng, _, journal = engine
        state = eng.states["nifty-atm-ce"]

        async def go():
            sig = sell()
            await eng._exit(state, sig, await feed_context(eng, sig))

        asyncio.run(go())
        assert eng.book.open_positions() == []
        assert list(journal.conn.execute("SELECT * FROM orders")) == []

    def test_the_exit_sizes_from_the_position(self, engine):
        """Config says 1 lot; if the book holds a different quantity the exit
        must follow the book, not the config."""
        eng, _, _ = engine
        state = eng.states["nifty-atm-ce"]

        async def go():
            entry = buy()
            await eng._enter(state, entry, await feed_context(eng, entry))
            position = eng.book.get(state.position_key)
            position.net_qty = 32          # simulate a partial fill
            eng.last_prices[state.position_key] = 150.0
            exit_sig = sell()
            await eng._exit(state, exit_sig, await feed_context(eng, exit_sig))

        asyncio.run(go())
        rows = list(_orders(eng))
        assert rows[-1]["quantity"] == 32


def _orders(eng):
    return eng.journal.conn.execute("SELECT * FROM orders ORDER BY id")


class TestGuardsBlockTheEngine:
    def test_a_halted_switch_stops_a_signal(self, engine):
        eng, notifier, journal = engine
        eng.killswitch.trip("test halt")
        state = eng.states["nifty-atm-ce"]

        asyncio.run(eng.handle_signal(state, buy()))
        assert eng.book.open_positions() == []
        assert notifier.saw("BLOCKED by kill_switch")

    def test_a_duplicate_signal_places_no_second_order(self, engine):
        eng, _, journal = engine
        state = eng.states["nifty-atm-ce"]
        sig = buy()

        async def go():
            for _ in range(2):
                ctx = await feed_context(eng, sig)
                verdict = eng.guards.check_signal(ctx)
                if verdict.allowed:
                    try:
                        journal.record_signal(sig)
                    except Exception:
                        return
                    await eng._enter(state, sig, ctx)

        asyncio.run(go())
        assert len(list(_orders(eng))) == 1

    def test_a_stale_feed_stops_a_signal(self, engine):
        eng, notifier, _ = engine
        state = eng.states["nifty-atm-ce"]

        async def go():
            sig = buy()
            ctx = await feed_context(eng, sig, tick_age=900.0)
            verdict = eng.guards.check_signal(ctx)
            assert not verdict.allowed
            assert verdict.guard == "feed_staleness"

        asyncio.run(go())
        assert eng.book.open_positions() == []


class TestFlatten:
    def test_flatten_closes_everything(self, engine):
        eng, notifier, _ = engine
        state = eng.states["nifty-atm-ce"]

        async def go():
            entry = buy()
            await eng._enter(state, entry, await feed_context(eng, entry))
            eng.last_prices[state.position_key] = 150.0
            await eng.flatten("test")

        asyncio.run(go())
        assert eng.book.open_positions() == []
        assert notifier.saw("FLATTENING")

    def test_flatten_with_nothing_open_is_a_no_op(self, engine):
        eng, notifier, _ = engine
        asyncio.run(eng.flatten("test"))
        assert not notifier.saw("FLATTENING")

    def test_an_orphan_position_is_reported_not_silently_skipped(self, engine):
        """A position no plan owns cannot be auto-closed, and the human needs to
        know rather than assume the flatten succeeded."""
        eng, notifier, _ = engine
        from orbot.models import Position
        eng.book.adopt([Position(security_id="99999",
                                 segment=ExchangeSegment.NSE_FNO, net_qty=50)])
        asyncio.run(eng.flatten("test"))
        assert notifier.saw("cannot flatten")


class TestHaltPropagation:
    def test_a_daily_loss_breach_halts_and_flattens(self, engine):
        eng, notifier, _ = engine
        state = eng.states["nifty-atm-ce"]

        async def go():
            entry = buy()
            await eng._enter(state, entry, await feed_context(eng, entry))
            eng.last_prices[state.position_key] = 150.0
            # Force the book deep underwater.
            eng.book.get(state.position_key).realised = -9_999.0
            sig = Signal(plan="nifty-atm-ce", action=Action.BUY,
                         bar_ts=NOW - dt.timedelta(seconds=10), price=24_300.0)
            ctx = await feed_context(eng, sig)
            verdict = eng.guards.check_signal(ctx)
            assert verdict.halt and verdict.flatten
            await eng._maybe_halt(verdict)

        asyncio.run(go())
        assert eng.killswitch.is_halted
        assert eng.book.open_positions() == []
        assert notifier.saw("TRADING HALTED")

    def test_a_halt_persists_for_a_restart(self, engine):
        eng, _, _ = engine
        eng.killswitch.trip("loss cap")
        assert KillSwitch(eng.killswitch.halt_file).is_halted
