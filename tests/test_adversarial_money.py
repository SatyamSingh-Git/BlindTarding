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
