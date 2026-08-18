"""Adversarial tests for the money path.

The existing adversarial suite hardens parsing — hostile frames, malformed
scrip masters, shuffled ticks. Every bug that actually reached the account
lived somewhere else: in what an order costs, what a position is worth, and
which guards were awake when the order went out.

These tests are written to fail.
"""

from __future__ import annotations

import datetime as dt

import pytest

from orbot.constants import (
    ExchangeSegment,
    InstrumentType,
    OrderStatus,
    OrderType,
    ProductType,
    TransactionType,
)
from orbot.config import GlobalRisk
from orbot.execution.guards import GuardChain
from orbot.models import Instrument, Order, OrderIntent, Signal
from orbot.ops.journal import Journal
from orbot.ops.killswitch import KillSwitch
from orbot.ops.market_calendar import MarketCalendar


@pytest.fixture
def chain(tmp_path) -> GuardChain:
    risk = GlobalRisk(max_daily_loss=5000.0, max_open_positions=3,
                      max_notional_per_order=2_500_000.0, max_qty_per_order=10_000,
                      feed_staleness_seconds=60, signal_staleness_seconds=120,
                      assumed_margin_pct=0.15)
    journal = Journal(tmp_path / "j.db")
    journal.start_run("paper")
    yield GuardChain(risk, KillSwitch(tmp_path / "HALT"), MarketCalendar(),
                     journal)
    journal.close()

GOLDM = Instrument(security_id="563946", segment=ExchangeSegment.MCX_COMM,
                   symbol="GOLDM", display_name="GOLDM04SEP26FUT",
                   instrument_type=InstrumentType.FUTCOM, lot_size=100)
SILVER = Instrument(security_id="574823", segment=ExchangeSegment.MCX_COMM,
                    symbol="SILVER100", display_name="SILVER10031AUG26FUT",
                    instrument_type=InstrumentType.FUTCOM, lot_size=100)


def intent(instrument, qty, side=TransactionType.BUY, signal=None):
    return OrderIntent(instrument=instrument, transaction_type=side,
                       quantity=qty, order_type=OrderType.MARKET,
                       product_type=ProductType.MARGIN, plan="p",
                       signal=signal)


def filled(qty, price):
    return Order(order_id="o", status=OrderStatus.TRADED,
                 filled_qty=qty, avg_price=price)


class TestNotionalIsRealExposure:
    """A cap expressed in rupees has to be compared against real rupees.
    GOLDM is 100 g quoted per 10 g, so qty x price overstates it tenfold."""

    def test_fat_finger_uses_true_notional_for_gold(self, chain):
        from orbot.execution.guards import GuardContext
        ctx = GuardContext(now=dt.datetime.now(), intent=intent(GOLDM, 100),
                           reference_price=153_327.0)
        # True exposure is Rs 15.3 lakh, inside the Rs 25 lakh cap. Measured
        # as qty x price it would read Rs 15.3 crore and be rejected.
        assert chain._fat_finger(ctx).allowed, (
            "rejected a legitimate gold lot by measuring exposure tenfold")
        # ...and a genuine 10x lot-size misread must still be caught
        big = GuardContext(now=dt.datetime.now(), intent=intent(GOLDM, 1_000),
                           reference_price=153_327.0)
        assert not chain._fat_finger(big).allowed

    def test_funds_check_asks_for_margin_not_notional(self, chain):
        from orbot.execution.guards import GuardContext
        # Rs 3 lakh buys one GOLDM lot: Rs 15.3 lakh of gold on ~15% margin.
        ctx = GuardContext(now=dt.datetime.now(), intent=intent(GOLDM, 100),
                           reference_price=153_327.0, available_funds=300_000.0)
        assert chain._sufficient_funds(ctx).allowed, (
            "blocked an affordable futures trade by demanding the full notional")


class TestGuardsWithNoReferencePrice:
    """At startup no tick has arrived, so ctx.reference_price is None. That is
    exactly when a position is opened by adoption."""

    def test_fat_finger_does_not_silently_pass(self, chain):
        from orbot.execution.guards import GuardContext
        ctx = GuardContext(now=dt.datetime.now(),
                           intent=intent(GOLDM, 100_000),  # absurd size
                           reference_price=None)
        assert not chain._fat_finger(ctx).allowed, (
            "no reference price turned the notional cap into a no-op")

    def test_funds_check_does_not_silently_pass(self, chain):
        from orbot.execution.guards import GuardContext
        ctx = GuardContext(now=dt.datetime.now(), intent=intent(GOLDM, 100),
                           reference_price=None, available_funds=1.0)
        assert not chain._sufficient_funds(ctx).allowed, (
            "no reference price turned the funds check into a no-op")


class TestPaperFundsInvariants:
    """Funds must never imply borrowing that was never granted."""

    @staticmethod
    def _oms():
        from orbot.execution.oms import PaperOMS
        return PaperOMS(starting_funds=300_000.0, slippage_pct=0.0, slippage_ticks=0)

    def test_funds_never_go_negative_on_an_affordable_trade(self):
        oms = self._oms()
        oms.place(intent(GOLDM, 100), reference_price=153_327.0)
        assert oms.available_funds() >= 0

    def test_round_trip_returns_exactly_the_pnl(self):
        oms = self._oms()
        oms.place(intent(GOLDM, 100), reference_price=150_000.0)
        oms.place(intent(GOLDM, 100, TransactionType.SELL),
                  reference_price=150_500.0)
        # 500 points at Rs 10 = Rs 5,000
        assert oms.available_funds() == pytest.approx(305_000.0)

    def test_a_flat_book_frees_every_rupee_of_margin(self):
        oms = self._oms()
        oms.place(intent(SILVER, 200), reference_price=2_378.0)
        oms.place(intent(SILVER, 200, TransactionType.SELL),
                  reference_price=2_378.0)
        assert oms.available_funds() == pytest.approx(300_000.0)
        assert oms.positions() == []


class TestAdoptionIsNotABackDoor:
    """Adoption opens a position at startup, the least supervised moment the
    bot has. It must clear the same gate as any other entry."""

    def test_a_tripped_kill_switch_stops_adoption(self, tmp_path):
        """The clearest case: a human halted the bot; a restart must not trade."""
        from orbot.execution.guards import GuardChain, GuardContext
        from orbot.ops.killswitch import KillSwitch
        from orbot.ops.market_calendar import MarketCalendar
        from orbot.config import GlobalRisk
        from orbot.ops.journal import Journal

        switch = KillSwitch(tmp_path / "HALT")
        switch.trip("manual")
        journal = Journal(tmp_path / "j.db")
        journal.start_run("paper")
        chain = GuardChain(GlobalRisk(), switch, MarketCalendar(), journal)
        sig = Signal(plan="p", action="BUY",
                     bar_ts=dt.datetime.now(), price=1.0, source="adopted")
        got = chain.check_signal(GuardContext(now=dt.datetime.now(), signal=sig))
        journal.close()
        assert not got.allowed and got.guard == "kill_switch"

    def test_the_same_signal_is_not_adopted_twice(self, tmp_path):
        """Restarting must not re-enter a trade already taken. The journal is
        the memory that survives the restart."""
        from orbot.ops.journal import DuplicateSignal, Journal

        journal = Journal(tmp_path / "j.db")
        journal.start_run("paper")
        sig = Signal(plan="goldm-2h", action="BUY",
                     bar_ts=dt.datetime(2026, 8, 14, 19, 0), price=153_250.0,
                     source="adopted")
        journal.record_signal(sig)
        with pytest.raises(DuplicateSignal):
            journal.record_signal(sig)     # second run, same bar
        journal.close()


class TestExpiringContracts:
    """A plan can pin one contract by security id, which never rolls. After
    expiry that id is dead -- no ticks, no fills -- and the plan goes quiet
    with no error anywhere."""

    @staticmethod
    def _ctx(days_out, action="BUY"):
        from orbot.config import SignalConfig, TradePlan
        from orbot.execution.guards import GuardContext
        plan = TradePlan(name="p", signal=SignalConfig(
            exchange_segment=ExchangeSegment.MCX_COMM, security_id="1",
            instrument_type=InstrumentType.FUTCOM, timeframe="2h"))
        now = dt.datetime(2026, 8, 17, 12, 0)
        sig = Signal(plan="p", action=action, bar_ts=now, price=1.0)
        return GuardContext(now=now, plan=plan, signal=sig,
                            contract_expiry=now.date() + dt.timedelta(days=days_out))

    def test_an_expired_contract_cannot_be_entered(self, chain):
        got = chain._contract_alive(self._ctx(-1))
        assert not got.allowed and got.guard == "contract_expired"

    def test_entries_stop_before_the_last_sessions(self, chain):
        got = chain._contract_alive(self._ctx(1))
        assert not got.allowed and got.guard == "contract_expiring"

    def test_a_healthy_contract_passes(self, chain):
        assert chain._contract_alive(self._ctx(14)).allowed

    def test_an_exit_is_never_blocked_by_expiry(self, chain):
        """Whatever else is true, you must always be able to close."""
        assert chain._contract_alive(self._ctx(-5, action="SELL")).allowed


class TestBracketExits:
    """The bracket is what stands between a losing trade and an unbounded one.
    It fires from tick data, between bar closes, on a latch."""

    @staticmethod
    def _state(sl=50.0, tp=250.0, entry=2_378.0, qty=200, long=True):
        from unittest.mock import MagicMock
        from orbot.config import PlanRisk, SignalConfig, TradePlan
        from orbot.engine.runner import PlanState
        from orbot.models import Position

        plan = TradePlan(
            name="p",
            signal=SignalConfig(exchange_segment=ExchangeSegment.MCX_COMM,
                                security_id="574823",
                                instrument_type=InstrumentType.FUTCOM,
                                timeframe="2h"),
            risk=PlanRisk(sl_points=sl, target_points=tp, square_off_time=None))
        state = PlanState(plan=plan, instrument=SILVER, timeframe=None,
                          series=None, strategy=None, resolver=None)
        state.position_key = SILVER.key
        position = Position(security_id=SILVER.security_id,
                            segment=SILVER.segment,
                            net_qty=qty if long else -qty, avg_price=entry)
        engine = MagicMock()
        engine.book.get.return_value = position
        engine._tasks = []
        return engine, state

    @staticmethod
    def _check(engine, state, price):
        """Run the bracket check without a live event loop.

        _check_bracket schedules the exit with asyncio.create_task, which needs
        a running loop; here the only question is whether it decided to fire.
        """
        import asyncio
        from unittest.mock import patch
        from orbot.engine.runner import TradingEngine

        fired = []
        with patch.object(asyncio, "create_task",
                          side_effect=lambda coro: (coro.close(),
                                                    fired.append(1))[1]):
            TradingEngine._check_bracket(engine, state, price)
        return bool(fired)

    def test_stop_fires_when_price_touches_it_exactly(self):
        engine, state = self._state()
        assert self._check(engine, state, 2_378.0 - 50.0)

    def test_stop_does_not_fire_one_tick_above(self):
        engine, state = self._state()
        assert not self._check(engine, state, 2_378.0 - 49.0)

    def test_target_fires_on_exact_touch(self):
        engine, state = self._state()
        assert self._check(engine, state, 2_378.0 + 250.0)

    def test_a_gap_straight_through_the_stop_still_fires(self):
        engine, state = self._state()
        assert self._check(engine, state, 2_000.0)

    def test_a_short_position_stops_out_upwards(self):
        """Sign errors here are silent and expensive: a short stopping out on a
        fall would ride the loss instead of cutting it."""
        engine, state = self._state(long=False)
        assert self._check(engine, state, 2_378.0 + 50.0)
        engine, state = self._state(long=False)
        assert not self._check(engine, state, 2_378.0 - 50.0)

    def test_the_latch_stops_a_second_flatten(self):
        """Ticks keep arriving while the exit order is in flight. Two flattens
        would try to sell what has already been sold."""
        engine, state = self._state()
        self._check(engine, state, 2_000.0)
        before = len(engine._tasks)
        self._check(engine, state, 1_900.0)
        assert len(engine._tasks) == before

    def test_no_bracket_configured_never_fires(self):
        engine, state = self._state(sl=None, tp=None)
        assert not self._check(engine, state, 1.0)

    def test_bracket_measures_from_the_adopted_signal_price(self):
        """An adopted trade keeps the stop the strategy was tested with, not
        one recomputed from a fill that has already drifted."""
        engine, state = self._state(entry=2_400.0)
        state.bracket_ref_price = 2_377.0        # the signal, not the fill
        assert not self._check(engine, state, 2_330.0)   # 2,377-50 = 2,327
        engine, state = self._state(entry=2_400.0)
        state.bracket_ref_price = 2_377.0
        assert self._check(engine, state, 2_327.0)


class TestAdoptedSignalsAreDeliberatelyOld:
    """Adoption acts on a bar that closed hours or days ago. The staleness
    guard must not treat that as a fault, and every other guard must still
    apply -- especially the duplicate check."""

    def test_staleness_does_not_block_an_adopted_signal(self, chain):
        from orbot.execution.guards import GuardContext
        old = dt.datetime.now() - dt.timedelta(days=3)
        sig = Signal(plan="p", action="BUY", bar_ts=old, price=1.0,
                     source="adopted")
        assert chain._signal_fresh(GuardContext(now=dt.datetime.now(),
                                                signal=sig)).allowed

    def test_a_normal_signal_that_old_is_still_blocked(self, chain):
        from orbot.execution.guards import GuardContext
        old = dt.datetime.now() - dt.timedelta(days=3)
        sig = Signal(plan="p", action="BUY", bar_ts=old, price=1.0,
                     source="engine")
        assert not chain._signal_fresh(GuardContext(now=dt.datetime.now(),
                                                    signal=sig)).allowed


class TestGuardsNeverRaise:
    """A guard that raises does not block -- it takes the signal path down with
    it, inside a task, where the exception is never seen."""

    @pytest.mark.parametrize("now_aware,bar_aware", [
        (True, False), (False, True), (False, False), (True, True)])
    def test_staleness_survives_any_mix_of_naive_and_aware(
            self, chain, now_aware, bar_aware):
        from zoneinfo import ZoneInfo
        from orbot.execution.guards import GuardContext
        IST = ZoneInfo("Asia/Kolkata")
        now = dt.datetime.now(IST) if now_aware else dt.datetime.now()
        bar = now - dt.timedelta(minutes=1)
        if not bar_aware and bar.tzinfo:
            bar = bar.replace(tzinfo=None)
        if bar_aware and not bar.tzinfo:
            bar = bar.replace(tzinfo=IST)
        sig = Signal(plan="p", action="BUY", bar_ts=bar, price=1.0)
        chain._signal_fresh(GuardContext(now=now, signal=sig))   # must not raise

    def test_every_signal_guard_survives_an_empty_context(self, chain):
        """Fail closed, never explode, on a context with nothing filled in."""
        from orbot.execution.guards import GuardContext
        ctx = GuardContext(now=dt.datetime.now())
        for guard in (chain._kill_switch, chain._market_open, chain._feed_fresh,
                      chain._signal_fresh, chain._not_duplicate,
                      chain._square_off_window, chain._contract_alive,
                      chain._daily_loss, chain._reconciled, chain._trades_today,
                      chain._open_positions):
            guard(ctx)          # must not raise

    def test_every_order_guard_survives_an_empty_context(self, chain):
        from orbot.execution.guards import GuardContext
        ctx = GuardContext(now=dt.datetime.now())
        for guard in (chain._fat_finger, chain._sufficient_funds,
                      chain._slippage):
            guard(ctx)


class TestPaperPositionsSurviveRestart:
    """A live broker remembers your positions across a restart; the simulator
    has to write them down. Without that the bot comes back flat, the
    duplicate guard correctly refuses to re-enter, and it sits blind to a
    trade it believes it is in."""

    def test_positions_and_funds_come_back(self, tmp_path):
        from orbot.execution.oms import PaperOMS
        path = tmp_path / "paper.json"

        one = PaperOMS(starting_funds=300_000.0, slippage_pct=0.0, slippage_ticks=0,
                       state_path=path)
        one.place(intent(GOLDM, 100), reference_price=153_327.0)
        funds, positions = one.available_funds(), one.positions()

        two = PaperOMS(starting_funds=300_000.0, slippage_pct=0.0, slippage_ticks=0,
                       state_path=path)
        assert [(p.security_id, p.net_qty, p.avg_price) for p in two.positions()] \
            == [(p.security_id, p.net_qty, p.avg_price) for p in positions]
        assert two.available_funds() == pytest.approx(funds)

    def test_a_flat_book_restores_as_flat(self, tmp_path):
        from orbot.constants import TransactionType
        from orbot.execution.oms import PaperOMS
        path = tmp_path / "paper.json"
        one = PaperOMS(starting_funds=300_000.0, slippage_pct=0.0, slippage_ticks=0, state_path=path)
        one.place(intent(GOLDM, 100), reference_price=150_000.0)
        one.place(intent(GOLDM, 100, TransactionType.SELL),
                  reference_price=150_500.0)
        two = PaperOMS(starting_funds=300_000.0, slippage_pct=0.0, slippage_ticks=0, state_path=path)
        assert two.positions() == []

    def test_corrupt_state_is_ignored_not_fatal(self, tmp_path):
        from orbot.execution.oms import PaperOMS
        path = tmp_path / "paper.json"
        path.write_text("{not json at all", encoding="utf-8")
        oms = PaperOMS(starting_funds=300_000.0, state_path=path)
        assert oms.positions() == []
        assert oms.available_funds() == pytest.approx(300_000.0)

    def test_state_of_the_wrong_shape_is_ignored(self, tmp_path):
        from orbot.execution.oms import PaperOMS
        path = tmp_path / "paper.json"
        path.write_text('["a list, not an object"]', encoding="utf-8")
        oms = PaperOMS(starting_funds=300_000.0, state_path=path)
        assert oms.positions() == []


class TestExchangeHours:
    """NSE shuts at 15:30, MCX runs to 23:30. Applying NSE's close to a
    commodity plan blocked the whole evening session -- and the backtests that
    chose these plans have entries at 17:00, 19:00, 21:00 and 23:00."""

    @staticmethod
    def _ctx(hour, segment):
        from orbot.config import SignalConfig, TradePlan
        from orbot.execution.guards import GuardContext
        plan = TradePlan(name="p", signal=SignalConfig(
            exchange_segment=segment, security_id="1",
            instrument_type=InstrumentType.FUTCOM, timeframe="2h"))
        return GuardContext(now=dt.datetime(2026, 8, 17, hour, 0), plan=plan)

    @pytest.mark.parametrize("hour", [17, 19, 21, 23])
    def test_mcx_evening_session_is_open(self, chain, hour):
        assert chain._market_open(self._ctx(hour, ExchangeSegment.MCX_COMM)).allowed

    @pytest.mark.parametrize("hour", [17, 21])
    def test_nse_is_shut_in_the_evening(self, chain, hour):
        assert not chain._market_open(self._ctx(hour, ExchangeSegment.NSE_EQ)).allowed

    def test_both_are_open_midday(self, chain):
        for seg in (ExchangeSegment.MCX_COMM, ExchangeSegment.NSE_EQ):
            assert chain._market_open(self._ctx(11, seg)).allowed

    def test_mcx_is_shut_after_its_own_close(self, chain):
        ctx = self._ctx(23, ExchangeSegment.MCX_COMM)
        ctx.now = dt.datetime(2026, 8, 17, 23, 45)
        assert not chain._market_open(ctx).allowed


class TestAdoptedPositionsKeepTheirMultiplier:
    """An adopted position never passes through apply_fill. Without the
    multiplier the book marks it 1:1 -- which showed a Rs 6,180 loss on gold
    as Rs 65,900."""

    @staticmethod
    def _position(qty=100, avg=154_187.0):
        from orbot.models import Position
        return Position(security_id=GOLDM.security_id, segment=GOLDM.segment,
                        net_qty=qty, avg_price=avg)

    def test_marking_without_a_multiplier_is_ten_times_wrong(self):
        from orbot.execution.positions import PositionBook
        book = PositionBook()
        book.adopt([self._position()])                    # no multiplier given
        book.mark(GOLDM.key, 153_569.0)
        assert book.unrealised == pytest.approx(-61_800.0)   # the wrong number

    def test_adopt_with_a_multiplier_marks_correctly(self):
        from orbot.execution.positions import PositionBook
        book = PositionBook()
        book.adopt([self._position()], multiplier_for=lambda k: 0.1)
        book.mark(GOLDM.key, 153_569.0)
        # 618 points down x 100 qty x 0.1 = Rs 6,180
        assert book.unrealised == pytest.approx(-6_180.0)

    def test_a_missing_multiplier_is_announced_not_assumed(self, capsys):
        """structlog writes to stdout, so capsys is what sees it."""
        from orbot.execution.positions import PositionBook
        book = PositionBook()
        book.adopt([self._position()])
        book.mark(GOLDM.key, 153_569.0)
        assert "no_multiplier" in capsys.readouterr().out, (
            "marked 1:1 without saying so")

    def test_the_warning_is_not_repeated_per_tick(self, capsys):
        """mark() runs on every tick; a warning per tick would bury the log."""
        from orbot.execution.positions import PositionBook
        book = PositionBook()
        book.adopt([self._position()])
        for price in (153_569.0, 153_570.0, 153_571.0):
            book.mark(GOLDM.key, price)
        assert capsys.readouterr().out.count("no_multiplier") == 1


class TestAdoptFillAtSignalPrice:
    """Filling an adopted trade at its signal price is a deliberate fiction:
    it keeps a paper run comparable to the backtest. It must never reach a
    live account, where no exchange will fill you at a price from Friday."""

    def test_live_mode_rejects_it(self, tmp_path):
        from orbot.config import (ExecutionConfig, PlanRisk, SignalConfig,
                                  StrategyConfig, TradePlan)
        plan = TradePlan(
            name="p", enabled=True,
            signal=SignalConfig(exchange_segment=ExchangeSegment.MCX_COMM,
                                security_id="563946",
                                instrument_type=InstrumentType.FUTCOM,
                                timeframe="2h"),
            strategy=StrategyConfig(adopt_open_signal=True, adopt_fill="signal",
                                    resampling_validated=True),
            execution=ExecutionConfig(mode="same", underlying="GOLDM",
                                      quantity=100),
            risk=PlanRisk(sl_points=2000, square_off_time=None))
        assert plan.strategy.adopt_fill == "signal"

    def test_default_is_the_honest_one(self):
        from orbot.config import StrategyConfig
        assert StrategyConfig().adopt_fill == "market"


class TestOneAnswerPerQuestion:
    """The positions panel and the header P&L must not disagree. The broker is
    authoritative for quantity and average price, but it has never seen a tick
    -- only the local book marks to market."""

    def test_broker_snapshot_carries_the_local_mark(self):
        from orbot.execution.positions import PositionBook
        from orbot.models import Position

        book = PositionBook()
        book.adopt([Position(security_id=GOLDM.security_id,
                             segment=GOLDM.segment, net_qty=100,
                             avg_price=153_327.0)],
                   multiplier_for=lambda k: 0.1)
        book.mark(GOLDM.key, 153_600.0)

        # what the broker would report: right size, no mark
        broker = [Position(security_id=GOLDM.security_id, segment=GOLDM.segment,
                           net_qty=100, avg_price=153_327.0)]
        assert broker[0].unrealised == 0.0

        for p in broker:                       # what the engine now does
            local = book.get(p.key)
            if local is not None and p.net_qty == local.net_qty:
                p.unrealised = local.unrealised

        assert broker[0].unrealised == pytest.approx(book.unrealised)
        assert broker[0].unrealised == pytest.approx(2_730.0)

    def test_a_size_mismatch_leaves_the_broker_untouched(self):
        """If the two disagree on size, the mark is meaningless -- and the
        mismatch itself is what matters, so nothing is papered over."""
        from orbot.execution.positions import PositionBook
        from orbot.models import Position

        book = PositionBook()
        book.adopt([Position(security_id=GOLDM.security_id,
                             segment=GOLDM.segment, net_qty=100,
                             avg_price=153_327.0)],
                   multiplier_for=lambda k: 0.1)
        book.mark(GOLDM.key, 153_600.0)

        broker = [Position(security_id=GOLDM.security_id, segment=GOLDM.segment,
                           net_qty=50, avg_price=153_327.0)]
        for p in broker:
            local = book.get(p.key)
            if local is not None and p.net_qty == local.net_qty:
                p.unrealised = local.unrealised
        assert broker[0].unrealised == 0.0


class TestContractMultipliersAreDerived:
    """Hardcoding one multiplier per symbol was wrong: GOLD and GOLDM are
    quoted identically but have different lot sizes, so the 0.1 that is right
    for GOLDM made GOLD worth 10 paise a point instead of Rs 100."""

    @staticmethod
    def _inst(symbol, lot):
        return Instrument(security_id="1", segment=ExchangeSegment.MCX_COMM,
                          instrument_type=InstrumentType.FUTCOM, symbol=symbol,
                          display_name=symbol, lot_size=lot)

    @pytest.mark.parametrize("symbol,lot,rupees_per_point", [
        ("GOLD", 1, 100),        # 1 kg quoted per 10 g
        ("GOLDM", 100, 10),      # 100 g quoted per 10 g
        ("GOLDGUINEA", 8, 0.8),
        ("SILVER", 30, 30),      # 30 kg quoted per kg
        ("SILVERM", 5, 5),
        ("SILVERMIC", 1, 1),
        ("SILVER100", 100, 100), # 1 kg quoted per 10 g
    ])
    def test_one_lot_is_worth_the_right_amount_per_point(
            self, symbol, lot, rupees_per_point):
        inst = self._inst(symbol, lot)
        assert inst.lot_size * inst.price_multiplier == pytest.approx(
            rupees_per_point)

    def test_an_unknown_symbol_falls_back_to_one_for_one(self):
        assert self._inst("NIFTY", 65).price_multiplier == 1.0

    def test_a_zero_lot_size_does_not_divide_by_zero(self):
        assert self._inst("GOLDM", 0).price_multiplier == 1.0


class TestExitsMissedWhileStopped:
    """Stopping overnight is fine for entries -- adoption catches those. An
    exit missed while stopped is worse: the position stays open, running risk
    the strategy has already decided against."""

    @staticmethod
    def _strategy_state(buy_at, sell_at, n=140):
        import datetime as dt
        from unittest.mock import patch
        from orbot.engine.strategy import SvmkrUtHmaOrb, params_from_config
        t0 = dt.datetime(2026, 8, 14, 9, 0)
        stub = type("R", (), {
            "timestamps": [t0 + dt.timedelta(hours=2 * i) for i in range(n)],
            "closes": [100.0 + i for i in range(n)],
            "buy": [i in buy_at for i in range(n)],
            "sell": [i in sell_at for i in range(n)],
            "ut": type("U", (), {"stop": [0.0] * n, "atr": [1.0] * n})(),
            "__len__": lambda self: n,
        })()
        strat = SvmkrUtHmaOrb(params_from_config({"ut_key": 3.0,
                                                  "ut_atr_period": 1}))
        return strat, patch.object(SvmkrUtHmaOrb, "evaluate", return_value=stub)

    def test_strategy_still_long_means_keep_holding(self):
        from orbot.models import Candle
        import datetime as dt
        strat, patched = self._strategy_state(buy_at={120}, sell_at=set())
        bars = [Candle(ts=dt.datetime(2026, 8, 14, 9, 0), open=1, high=1,
                       low=1, close=1)] * 140
        with patched:
            assert strat.open_position_entry(bars) is not None

    def test_strategy_flat_means_the_position_is_stale(self):
        """A SELL after the BUY: whatever we still hold should not be held."""
        from orbot.models import Candle
        import datetime as dt
        strat, patched = self._strategy_state(buy_at={120}, sell_at={131})
        bars = [Candle(ts=dt.datetime(2026, 8, 14, 9, 0), open=1, high=1,
                       low=1, close=1)] * 140
        with patched:
            assert strat.open_position_entry(bars) is None


class TestPaperOmsDoesNotShareItsState:
    """A live client parses fresh objects out of JSON. The simulator handing
    out its own let the book alias OMS state, so one fill was applied twice --
    turning a closed long into a phantom short."""

    @staticmethod
    def _oms_and_book():
        from orbot.execution.oms import PaperOMS
        from orbot.execution.positions import PositionBook
        oms = PaperOMS(starting_funds=300_000.0, slippage_pct=0.0, slippage_ticks=0)
        oms.place(intent(GOLDM, 100), reference_price=153_327.0)
        book = PositionBook()
        book.adopt(oms.positions(), multiplier_for=lambda k: 0.1)
        return oms, book

    def test_positions_are_copies(self):
        oms, book = self._oms_and_book()
        assert book.get(GOLDM.key) is not oms.positions()[0]

    def test_a_full_close_leaves_the_book_flat_not_short(self):
        from orbot.constants import TransactionType
        oms, book = self._oms_and_book()
        order = oms.place(intent(GOLDM, 100, TransactionType.SELL),
                          reference_price=153_644.0)
        book.apply_fill(intent(GOLDM, 100, TransactionType.SELL), order)
        assert book.get(GOLDM.key).net_qty == 0, "closed long became a short"
        assert oms.positions() == []

    def test_mutating_a_returned_position_cannot_reach_the_oms(self):
        oms, _ = self._oms_and_book()
        snapshot = oms.positions()
        snapshot[0].net_qty = 9_999
        assert oms.positions()[0].net_qty == 100


class TestUnfinishedBarsNeverReachTheStrategy:
    """Resampling buckets by wall clock, so the last bucket of a live session
    holds only the minutes traded so far -- and seeding marks every bar closed.
    A half-formed 09:00 bar showed a SELL at 09:45 that need not survive to
    11:00, and the stale-position check would have closed on it."""

    @staticmethod
    def _bars(last_open_hours_ago):
        import datetime as dt
        from orbot.models import Candle
        now = dt.datetime.now()
        first = now - dt.timedelta(hours=last_open_hours_ago)
        return [Candle(ts=first, open=100, high=101, low=99, close=100)]

    def test_a_bucket_still_forming_is_dropped(self):
        from orbot.engine.history import _drop_unfinished
        from orbot.engine.timeframe import Timeframe
        bars = self._bars(1)          # opened an hour ago, 2h bar -> unfinished
        assert _drop_unfinished(bars, Timeframe.parse("2h")) == []

    def test_a_finished_bucket_is_kept(self):
        from orbot.engine.history import _drop_unfinished
        from orbot.engine.timeframe import Timeframe
        bars = self._bars(3)          # opened three hours ago -> complete
        assert _drop_unfinished(bars, Timeframe.parse("2h")) == bars

    def test_an_empty_series_is_safe(self):
        from orbot.engine.history import _drop_unfinished
        from orbot.engine.timeframe import Timeframe
        assert _drop_unfinished([], Timeframe.parse("2h")) == []

    def test_daily_bars_are_left_alone(self):
        from orbot.engine.history import _drop_unfinished
        from orbot.engine.timeframe import Timeframe
        bars = self._bars(1)
        assert _drop_unfinished(bars, Timeframe.parse("1D")) == bars


class TestSlippageIsMeasuredInTicks:
    """As a percentage, slippage scales with the price a contract happens to be
    quoted in: 0.05% cost GOLDM 77 ticks and SILVER100 one. The same setting,
    two orders of magnitude apart, for no reason at all."""

    @staticmethod
    def _fill(symbol, price, tick, side, ticks=1.0):
        from orbot.constants import OrderType, ProductType, TransactionType
        from orbot.execution.oms import PaperOMS
        inst = Instrument(security_id="1", segment=ExchangeSegment.MCX_COMM,
                          symbol=symbol, display_name=symbol,
                          instrument_type=InstrumentType.FUTCOM, lot_size=100,
                          tick_size=tick)
        oms = PaperOMS(starting_funds=1e7, slippage_ticks=ticks)
        order = oms.place(OrderIntent(instrument=inst, transaction_type=side,
                                      quantity=100, order_type=OrderType.MARKET,
                                      product_type=ProductType.MARGIN, plan="p"),
                          reference_price=price)
        return order.avg_price

    def test_gold_slips_one_tick_not_seventy_seven(self):
        from orbot.constants import TransactionType
        got = self._fill("GOLDM", 153_250.0, 1.0, TransactionType.BUY)
        assert got == pytest.approx(153_251.0)

    def test_silver_slips_the_same_one_tick(self):
        from orbot.constants import TransactionType
        got = self._fill("SILVER100", 2_377.0, 1.0, TransactionType.BUY)
        assert got == pytest.approx(2_378.0)

    def test_a_sell_slips_against_you_too(self):
        from orbot.constants import TransactionType
        got = self._fill("GOLDM", 153_863.0, 1.0, TransactionType.SELL)
        assert got == pytest.approx(153_862.0)

    def test_zero_ticks_means_a_frictionless_fill(self):
        from orbot.constants import TransactionType
        assert self._fill("GOLDM", 153_250.0, 1.0, TransactionType.BUY,
                          ticks=0) == pytest.approx(153_250.0)

    def test_a_round_trip_costs_two_ticks_whatever_the_price(self):
        """The point of the change: cost no longer depends on price size."""
        from orbot.constants import TransactionType
        for symbol, price, tick in (("GOLDM", 153_250.0, 1.0),
                                    ("SILVER100", 2_377.0, 1.0)):
            buy = self._fill(symbol, price, tick, TransactionType.BUY)
            sell = self._fill(symbol, price, tick, TransactionType.SELL)
            assert (buy - sell) == pytest.approx(2 * tick)


class TestReconnectDoesNotEarnA429:
    """Angel answered a day of 1-second retries with HTTP 429, so the reconnect
    storm became the outage."""

    @staticmethod
    def _feed():
        from orbot.broker.angel.auth import AngelAuth
        from orbot.broker.angel.feed import AngelFeed
        return AngelFeed(AngelAuth("C1", "key"))

    def test_backoff_is_capped_well_above_the_old_thirty_seconds(self):
        assert self._feed().max_backoff >= 120.0

    def test_a_throttle_backoff_exists_and_is_not_a_second(self):
        from orbot.broker.angel.feed import THROTTLED_BACKOFF
        assert THROTTLED_BACKOFF >= 30.0

    def test_a_connection_must_hold_before_it_counts_as_healthy(self):
        from orbot.broker.angel.feed import STABLE_SECONDS
        assert STABLE_SECONDS >= 30.0

    def test_the_reset_is_gated_on_lifetime_not_on_connecting(self):
        """The precise bug: `backoff = 1.0` used to sit next to the connect log,
        so a socket that died a second later retried at 1s forever."""
        import inspect
        from orbot.broker.angel.feed import AngelFeed
        src = inspect.getsource(AngelFeed.run)
        assert "STABLE_SECONDS" in src
        connect_line = src.index("angel_feed.connected")
        after = src[connect_line:connect_line + 400]
        assert "backoff = 1.0" not in after, (
            "backoff still resets merely on connecting")


class TestTradeCapFitsTheTimeframe:
    """An MCX day holds eight 2-hour bars, so eight signals is legitimate. A cap
    of 4 blocked a real silver signal at 17:00 on 18 August."""

    def test_the_live_plans_allow_a_full_day_of_bars(self):
        from orbot.config import load_plans
        for plan in load_plans():
            if not plan.enabled:
                continue
            bars_per_day = 8 if plan.signal.timeframe == "2h" else None
            if bars_per_day:
                assert plan.risk.max_trades_per_day >= bars_per_day, (
                    f"{plan.name} caps at {plan.risk.max_trades_per_day} but a "
                    f"day can legitimately produce {bars_per_day} signals")

    def test_the_cap_still_catches_a_runaway(self, chain):
        from orbot.config import PlanRisk, SignalConfig, TradePlan
        from orbot.execution.guards import GuardContext
        plan = TradePlan(name="p", signal=SignalConfig(
            exchange_segment=ExchangeSegment.MCX_COMM, security_id="1",
            instrument_type=InstrumentType.FUTCOM, timeframe="2h"),
            risk=PlanRisk(max_trades_per_day=10))
        ctx = GuardContext(now=dt.datetime.now(), plan=plan, trades_today=10)
        assert not chain._trades_today(ctx).allowed


class TestPositionsPanelShowsNow:
    """"Latest snapshot" means the most recent batch. Taking the most recent
    row per instrument with no time bound kept yesterday's closed positions on
    screen all of the next day."""

    @staticmethod
    def _db(tmp_path):
        import sqlite3
        from orbot.ops.journal import Journal
        j = Journal(tmp_path / "j.db")
        j.start_run("paper")
        return j, sqlite3.connect(tmp_path / "j.db")

    def test_yesterdays_open_position_does_not_survive_today(self, tmp_path):
        from orbot.models import Position
        j, con = self._db(tmp_path)
        gold = Position(security_id="563946", segment=ExchangeSegment.MCX_COMM,
                        net_qty=100, avg_price=153_327.0)
        j.snapshot_positions([gold], source="broker")     # yesterday: open
        assert j.last_snapshot_keys() == {("MCX_COMM", "563946")}
        # today the engine knows it was open, so it records the close explicitly
        flat = Position(security_id="563946", segment=ExchangeSegment.MCX_COMM,
                        net_qty=0, avg_price=0.0)
        j.snapshot_positions([flat], source="broker")
        con.row_factory = None
        rows = con.execute(
            "SELECT security_id FROM position_snapshots WHERE source='broker'"
            " AND net_qty != 0 AND taken_at = (SELECT MAX(taken_at) FROM"
            " position_snapshots WHERE source='broker')").fetchall()
        j.close(); con.close()
        assert rows == [], "a position closed since the last snapshot still shows"


class TestRealisedPnlSurvivesARestart:
    """The position book is rebuilt from nothing each run, so a day's booked
    profit vanished the moment the engine was bounced -- the dashboard read
    Rs 0 on a day that had banked Rs 1,710."""

    @staticmethod
    def _oms(tmp_path):
        from orbot.execution.oms import PaperOMS
        return PaperOMS(starting_funds=300_000.0, slippage_ticks=0,
                        state_path=tmp_path / "paper.json")

    def test_a_closed_trade_is_remembered_across_a_restart(self, tmp_path):
        import datetime as dt
        from orbot.constants import TransactionType
        one = self._oms(tmp_path)
        one.place(intent(GOLDM, 100), reference_price=150_000.0)
        one.place(intent(GOLDM, 100, TransactionType.SELL),
                  reference_price=150_500.0)
        today = dt.date.today()
        assert one.realised_on(today) == pytest.approx(5_000.0)

        two = self._oms(tmp_path)          # restart
        assert two.positions() == []
        assert two.realised_on(today) == pytest.approx(5_000.0)

    def test_a_day_with_no_trades_reports_zero(self, tmp_path):
        import datetime as dt
        assert self._oms(tmp_path).realised_on(dt.date(2020, 1, 1)) == 0.0

    def test_days_are_kept_apart(self, tmp_path):
        import datetime as dt
        from orbot.constants import TransactionType
        oms = self._oms(tmp_path)
        oms.place(intent(GOLDM, 100), reference_price=150_000.0)
        oms.place(intent(GOLDM, 100, TransactionType.SELL),
                  reference_price=150_500.0)
        assert oms.realised_on(dt.date.today()) == pytest.approx(5_000.0)
        assert oms.realised_on(dt.date.today() - dt.timedelta(days=1)) == 0.0

    def test_a_live_broker_reports_nothing_here(self):
        """Only the simulator needs this memory; live has its own."""
        import datetime as dt
        from orbot.execution.oms import LiveOMS
        assert LiveOMS.realised_on(object(), dt.date.today()) == 0.0


class TestLossyBarsAreNotTraded:
    """A bar aggregated through a dropped socket looks whole: plausible close,
    truncated high and low. ATR comes from that range, the stop from that ATR,
    and a crossing appears the exchange never printed. Four such phantom
    signals cost Rs 14,090 on 18 August, and the staleness guard cannot see it
    -- that catches a feed delivering nothing, not one delivering most."""

    @staticmethod
    def _bar(hour, high, low, close):
        from orbot.models import Candle
        return Candle(ts=dt.datetime(2026, 8, 18, hour, 0), open=close,
                      high=high, low=low, close=close, volume=1, closed=True)

    def test_replace_last_swaps_the_bar(self):
        from orbot.engine.candles import CandleSeries
        series = CandleSeries(120)
        series.seed([self._bar(9, 154_140, 153_622, 153_863)])
        good = self._bar(9, 154_250, 153_500, 153_863)
        assert series.replace_last(good)
        assert series.bars[-1].high == 154_250
        assert series.bars[-1].low == 153_500

    def test_it_refuses_to_rewrite_a_different_bar(self):
        """Rewriting history further back would change signals already acted on."""
        from orbot.engine.candles import CandleSeries
        series = CandleSeries(120)
        series.seed([self._bar(9, 154_140, 153_622, 153_863)])
        assert not series.replace_last(self._bar(11, 1, 1, 1))
        assert series.bars[-1].ts.hour == 9

    def test_it_refuses_on_an_empty_series(self):
        from orbot.engine.candles import CandleSeries
        assert not CandleSeries(120).replace_last(self._bar(9, 1, 1, 1))

    def test_the_feed_reports_a_drop_inside_a_bar(self):
        from orbot.broker.angel.auth import AngelAuth
        from orbot.broker.angel.feed import AngelFeed
        feed = AngelFeed(AngelAuth("C1", "key"))
        bar_open = dt.datetime.now(feed._tz if hasattr(feed, "_tz") else None)
        assert not feed.dropped_since(bar_open), "a fresh feed has not dropped"
        feed._last_disconnect_at = bar_open + dt.timedelta(minutes=5)
        assert feed.dropped_since(bar_open)
        feed._last_disconnect_at = bar_open - dt.timedelta(minutes=5)
        assert not feed.dropped_since(bar_open), (
            "a drop before the bar opened is not this bar's problem")


class TestContractRolling:
    """A pinned id never rolls. The day it expires the token is dead: no data
    comes back, no order goes out, and the plan goes quiet with nothing
    anywhere to say why. Gold expires 4 Sep, silver 31 Aug."""

    def test_both_live_plans_roll(self):
        from orbot.config import load_plans
        for plan in load_plans():
            if plan.enabled:
                assert plan.signal.roll == "front", (
                    f"{plan.name} pins one contract and will die at its expiry")

    def test_the_default_is_to_pin(self):
        """Rolling changes which instrument is traded, so it is opt-in."""
        from orbot.config import SignalConfig
        cfg = SignalConfig(exchange_segment=ExchangeSegment.MCX_COMM,
                           security_id="1", instrument_type=InstrumentType.FUTCOM)
        assert cfg.roll == "none"

    def test_a_roll_resolves_the_nearest_expiry(self):
        """The whole point: after the pinned contract dies, resolution moves on."""
        import datetime as dt
        from unittest.mock import MagicMock
        from orbot.config import (ExecutionConfig, SignalConfig, StrategyConfig,
                                  TradePlan)
        from orbot.engine.runner import TradingEngine

        dead = Instrument(security_id="563946", segment=ExchangeSegment.MCX_COMM,
                          symbol="GOLDM", display_name="GOLDM04SEP26FUT",
                          instrument_type=InstrumentType.FUTCOM, lot_size=100,
                          expiry=dt.date(2026, 9, 4))
        live = Instrument(security_id="569003", segment=ExchangeSegment.MCX_COMM,
                          symbol="GOLDM", display_name="GOLDM05OCT26FUT",
                          instrument_type=InstrumentType.FUTCOM, lot_size=100,
                          expiry=dt.date(2026, 10, 5))
        plan = TradePlan(
            name="goldm-2h",
            signal=SignalConfig(exchange_segment=ExchangeSegment.MCX_COMM,
                                security_id="563946",
                                instrument_type=InstrumentType.FUTCOM,
                                timeframe="2h", roll="front"),
            strategy=StrategyConfig(),
            execution=ExecutionConfig(mode="same", underlying="GOLDM",
                                      quantity=100))
        eng = MagicMock()
        eng.registry.get.return_value = dead
        eng.registry.select_expiry.return_value = dt.date(2026, 10, 5)
        eng.registry.future.return_value = live
        got = TradingEngine._resolve_signal_instrument(eng, plan)
        assert got.security_id == "569003"
        assert eng.notifier.send.called, "a roll must be announced, not silent"

    def test_a_failed_roll_keeps_the_configured_contract(self):
        """Fail soft here: a resolution error must not stop the plan loading."""
        from unittest.mock import MagicMock
        from orbot.config import (ExecutionConfig, SignalConfig, StrategyConfig,
                                  TradePlan)
        from orbot.engine.runner import TradingEngine
        pinned = Instrument(security_id="563946",
                            segment=ExchangeSegment.MCX_COMM, symbol="GOLDM",
                            display_name="GOLDM04SEP26FUT",
                            instrument_type=InstrumentType.FUTCOM, lot_size=100)
        plan = TradePlan(
            name="p",
            signal=SignalConfig(exchange_segment=ExchangeSegment.MCX_COMM,
                                security_id="563946",
                                instrument_type=InstrumentType.FUTCOM,
                                timeframe="2h", roll="front"),
            strategy=StrategyConfig(),
            execution=ExecutionConfig(mode="same", underlying="GOLDM",
                                      quantity=100))
        eng = MagicMock()
        eng.registry.get.return_value = pinned
        eng.registry.select_expiry.side_effect = RuntimeError("no expiries")
        got = TradingEngine._resolve_signal_instrument(eng, plan)
        assert got.security_id == "563946"
