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
        return PaperOMS(starting_funds=300_000.0, slippage_pct=0.0)

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
