"""Risk guard tests.

These are the tests that matter most in the whole suite. A bug in the strategy
costs a bad trade; a bug in a guard removes the thing that was supposed to stop
a bad afternoon becoming a bad month.

Every guard is tested for three properties: it blocks what it should, it allows
what it should, and — where the answer is unknown — it **fails closed**. That
last one is the easiest to get wrong, because failing open looks like it works
in every test where the data is present.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from orbot.config import GlobalRisk, PlanRisk, SignalConfig, TradePlan
from orbot.constants import (
    ExchangeSegment,
    InstrumentType,
    OrderType,
    ProductType,
    TransactionType,
)
from orbot.execution.guards import GuardChain, GuardContext
from orbot.models import Action, Instrument, OrderIntent, Position, Signal
from orbot.ops.journal import Journal
from orbot.ops.killswitch import KillSwitch
from orbot.ops.market_calendar import MarketCalendar

IST = ZoneInfo("Asia/Kolkata")
OPEN_TIME = dt.datetime(2026, 8, 14, 11, 0, tzinfo=IST)      # a Friday, mid-session


@pytest.fixture
def risk() -> GlobalRisk:
    return GlobalRisk(
        max_daily_loss=5000.0, max_open_positions=3,
        max_notional_per_order=200_000.0, max_qty_per_order=10_000,
        feed_staleness_seconds=60, signal_staleness_seconds=120,
    )


@pytest.fixture
def journal(tmp_path: Path) -> Journal:
    j = Journal(tmp_path / "j.db")
    j.start_run("paper")
    yield j
    j.close()


@pytest.fixture
def switch(tmp_path: Path) -> KillSwitch:
    return KillSwitch(tmp_path / "HALT")


@pytest.fixture
def chain(risk, switch, journal) -> GuardChain:
    return GuardChain(risk, switch, MarketCalendar(), journal)


def make_plan(*, max_trades: int = 4,
              square_off: dt.time = dt.time(15, 10)) -> TradePlan:
    return TradePlan(
        name="test-plan",
        signal=SignalConfig(exchange_segment=ExchangeSegment.IDX_I,
                            security_id="13", timeframe="5m"),
        risk=PlanRisk(max_trades_per_day=max_trades, square_off_time=square_off),
    )


def make_signal(*, action: str = Action.BUY,
                bar_ts: dt.datetime | None = None, price: float = 24_500.0) -> Signal:
    return Signal(plan="test-plan", action=action,
                  bar_ts=bar_ts or (OPEN_TIME - dt.timedelta(seconds=30)),
                  price=price)


INSTRUMENT = Instrument(
    security_id="45102", segment=ExchangeSegment.NSE_FNO,
    instrument_type=InstrumentType.OPTIDX, symbol="NIFTY",
    display_name="NIFTY 20 AUG 24300 CALL", lot_size=65, tick_size=0.05,
)


def make_intent(*, quantity: int = 65, price: float = 120.0,
                order_type: OrderType = OrderType.MARKET,
                signal: Signal | None = None) -> OrderIntent:
    return OrderIntent(plan="test-plan", instrument=INSTRUMENT,
                       transaction_type=TransactionType.BUY, quantity=quantity,
                       product_type=ProductType.INTRADAY, order_type=order_type,
                       price=price, signal=signal)


def ctx(**overrides) -> GuardContext:
    base = dict(now=OPEN_TIME, plan=make_plan(), signal=make_signal(),
                last_tick_age=2.0, available_funds=500_000.0)
    base.update(overrides)
    return GuardContext(**base)


# =====================================================================
class TestHappyPath:
    def test_a_clean_signal_passes_every_guard(self, chain):
        assert chain.check_signal(ctx()).allowed

    def test_a_clean_order_passes_every_guard(self, chain):
        assert chain.check_order(ctx(intent=make_intent(), reference_price=120.0)).allowed


class TestKillSwitch:
    """Guard 1."""

    def test_a_tripped_switch_blocks(self, chain, switch):
        switch.trip("manual test")
        result = chain.check_signal(ctx())
        assert not result.allowed and result.guard == "kill_switch"

    def test_the_file_alone_blocks(self, chain, switch):
        """Works even if the process never tripped it — a human with a shell can
        stop the bot without its cooperation."""
        switch.halt_file.write_text("stop", encoding="utf-8")
        assert not chain.check_signal(ctx()).allowed

    def test_clearing_re_permits(self, chain, switch):
        switch.trip("x")
        switch.clear()
        assert chain.check_signal(ctx()).allowed

    def test_order_stage_rechecks_it(self, chain, switch):
        """A human may halt between the signal passing and the order going out."""
        switch.trip("halted mid-flight")
        assert not chain.check_order(ctx(intent=make_intent())).allowed

    def test_a_persisted_trip_survives_a_new_instance(self, tmp_path):
        """A daily-loss halt must not be cleared by a crash-restart, or a
        crash-loop becomes an unlimited-loss loop."""
        path = tmp_path / "HALT"
        KillSwitch(path).trip("loss cap", persist=True)
        assert KillSwitch(path).is_halted


class TestMarketHours:
    """Guard 7."""

    @pytest.mark.parametrize("when,why", [
        (dt.datetime(2026, 8, 14, 9, 0, tzinfo=IST), "pre-open auction"),
        (dt.datetime(2026, 8, 14, 15, 45, tzinfo=IST), "after close"),
        (dt.datetime(2026, 8, 15, 11, 0, tzinfo=IST), "Saturday"),
        (dt.datetime(2026, 8, 16, 11, 0, tzinfo=IST), "Sunday"),
    ])
    def test_closed_market_blocks(self, chain, when, why):
        result = chain.check_signal(ctx(now=when))
        assert not result.allowed, why
        assert result.guard == "market_hours"

    def test_configured_holiday_blocks(self, risk, switch, journal):
        calendar = MarketCalendar(holidays={dt.date(2026, 8, 14)})
        chain = GuardChain(risk, switch, calendar, journal)
        assert not chain.check_signal(ctx()).allowed

    def test_a_special_session_overrides_a_weekend(self, risk, switch, journal):
        """Muhurat trading runs on a Saturday evening. The signal's bar has to
        move with `now`, or the staleness guard fires first — as it did when
        this test was first written, which is the guard behaving correctly."""
        muhurat = dt.datetime(2026, 8, 15, 18, 30, tzinfo=IST)   # a Saturday
        calendar = MarketCalendar(special_sessions={
            dt.date(2026, 8, 15): (dt.time(18, 15), dt.time(19, 15))})
        chain = GuardChain(risk, switch, calendar, journal)
        fresh = make_signal(bar_ts=muhurat - dt.timedelta(seconds=20))
        # The plan's square-off cutoff has to move with the session too, or
        # guard 12 blocks every muhurat entry as "past the cutoff".
        evening_plan = make_plan(square_off=dt.time(19, 0))
        assert chain.check_signal(
            ctx(now=muhurat, signal=fresh, plan=evening_plan)).allowed

    def test_a_special_session_still_respects_its_own_hours(self, risk, switch, journal):
        before = dt.datetime(2026, 8, 15, 17, 0, tzinfo=IST)
        calendar = MarketCalendar(special_sessions={
            dt.date(2026, 8, 15): (dt.time(18, 15), dt.time(19, 15))})
        chain = GuardChain(risk, switch, calendar, journal)
        fresh = make_signal(bar_ts=before - dt.timedelta(seconds=20))
        evening_plan = make_plan(square_off=dt.time(19, 0))
        result = chain.check_signal(
            ctx(now=before, signal=fresh, plan=evening_plan))
        assert not result.allowed and result.guard == "market_hours"


class TestFeedStaleness:
    """Guard 6 — trading on a frozen feed is the classic silent disaster."""

    def test_a_stale_feed_blocks(self, chain):
        result = chain.check_signal(ctx(last_tick_age=120.0))
        assert not result.allowed and result.guard == "feed_staleness"

    def test_no_tick_at_all_blocks(self, chain):
        """Fails closed. A feed that has never delivered is not proven working,
        and 'no data yet' must not read as 'data is fine'."""
        result = chain.check_signal(ctx(last_tick_age=None))
        assert not result.allowed and result.guard == "feed_staleness"

    def test_a_fresh_feed_passes(self, chain):
        assert chain.check_signal(ctx(last_tick_age=5.0)).allowed

    def test_the_boundary_is_inclusive_of_the_limit(self, chain):
        assert chain.check_signal(ctx(last_tick_age=60.0)).allowed
        assert not chain.check_signal(ctx(last_tick_age=60.1)).allowed


class TestSignalStaleness:
    """Guard 14 — catches restart replays and late retries."""

    def test_an_old_bar_blocks(self, chain):
        old = make_signal(bar_ts=OPEN_TIME - dt.timedelta(minutes=30))
        result = chain.check_signal(ctx(signal=old))
        assert not result.allowed and result.guard == "signal_staleness"

    def test_a_recent_bar_passes(self, chain):
        fresh = make_signal(bar_ts=OPEN_TIME - dt.timedelta(seconds=10))
        assert chain.check_signal(ctx(signal=fresh)).allowed

    def test_a_future_bar_blocks(self, chain):
        """A bar timestamped ahead of now means a clock or timezone fault, and
        acting on it would be acting on a bar that has not closed."""
        future = make_signal(bar_ts=OPEN_TIME + dt.timedelta(minutes=5))
        result = chain.check_signal(ctx(signal=future))
        assert not result.allowed and result.guard == "signal_staleness"


class TestDuplicateSuppression:
    """Guard 5 — backed by a UNIQUE constraint so it survives a restart."""

    def test_a_repeated_signal_blocks(self, chain, journal):
        signal = make_signal()
        journal.record_signal(signal)
        result = chain.check_signal(ctx(signal=signal))
        assert not result.allowed and result.guard == "duplicate_signal"

    def test_a_different_bar_passes(self, chain, journal):
        journal.record_signal(make_signal())
        other = make_signal(bar_ts=OPEN_TIME - dt.timedelta(seconds=5))
        assert chain.check_signal(ctx(signal=other)).allowed

    def test_the_opposite_action_on_the_same_bar_passes(self, chain, journal):
        journal.record_signal(make_signal(action=Action.BUY))
        sell = make_signal(action=Action.SELL)
        assert chain.check_signal(ctx(signal=sell)).allowed

    def test_dedupe_survives_a_new_journal_on_the_same_file(self, tmp_path, risk, switch):
        """The restart case. An in-memory set would forget and re-fire."""
        path = tmp_path / "j.db"
        first = Journal(path)
        first.start_run("paper")
        signal = make_signal()
        first.record_signal(signal)
        first.close()

        second = Journal(path)
        chain = GuardChain(risk, switch, MarketCalendar(), second)
        try:
            assert not chain.check_signal(ctx(signal=signal)).allowed
        finally:
            second.close()


class TestDailyLossCap:
    """Guard 2 — halts and flattens."""

    def test_a_breach_blocks_halts_and_flattens(self, chain):
        result = chain.check_signal(ctx(realised_pnl=-5200.0))
        assert not result.allowed
        assert result.guard == "daily_loss_cap"
        assert result.halt and result.flatten

    def test_realised_and_unrealised_are_summed(self, chain):
        """An open position deep underwater counts. Waiting for it to be
        realised is how a loss cap gets bypassed."""
        result = chain.check_signal(ctx(realised_pnl=-3000.0, unrealised_pnl=-2500.0))
        assert not result.allowed and result.halt

    def test_within_the_cap_passes(self, chain):
        assert chain.check_signal(ctx(realised_pnl=-4999.0)).allowed

    def test_a_profit_passes(self, chain):
        assert chain.check_signal(ctx(realised_pnl=50_000.0)).allowed

    def test_exactly_at_the_cap_blocks(self, chain):
        assert not chain.check_signal(ctx(realised_pnl=-5000.0)).allowed


class TestReconciliation:
    """Guard 11 — a disagreement means our model of reality is wrong."""

    def test_a_mismatch_blocks_and_halts(self, chain):
        result = chain.check_signal(
            ctx(reconciled=False, reconcile_detail="local +65 vs broker 0"))
        assert not result.allowed
        assert result.guard == "position_reconciliation"
        assert result.halt

    def test_agreement_passes(self, chain):
        assert chain.check_signal(ctx(reconciled=True)).allowed


class TestTradeAndPositionCaps:
    """Guards 3 and 4."""

    def test_the_daily_trade_cap_blocks(self, chain):
        result = chain.check_signal(ctx(trades_today=4))
        assert not result.allowed and result.guard == "max_trades_per_day"

    def test_below_the_trade_cap_passes(self, chain):
        assert chain.check_signal(ctx(trades_today=3)).allowed

    def test_the_open_position_cap_blocks(self, chain):
        result = chain.check_signal(ctx(open_position_count=3))
        assert not result.allowed and result.guard == "max_open_positions"

    def test_a_zero_cap_means_unlimited(self, risk, switch, journal):
        risk.max_open_positions = 0
        chain = GuardChain(risk, switch, MarketCalendar(), journal)
        assert chain.check_signal(ctx(open_position_count=99)).allowed


class TestSquareOffWindow:
    """Guard 12 — no new entries that will be force-closed minutes later."""

    def test_after_the_cutoff_blocks(self, chain):
        late = dt.datetime(2026, 8, 14, 15, 12, tzinfo=IST)
        result = chain.check_signal(ctx(now=late,
                                        signal=make_signal(bar_ts=late)))
        assert not result.allowed and result.guard == "square_off_window"

    def test_before_the_cutoff_passes(self, chain):
        early = dt.datetime(2026, 8, 14, 15, 5, tzinfo=IST)
        assert chain.check_signal(ctx(now=early,
                                      signal=make_signal(bar_ts=early))).allowed


class TestFatFinger:
    """Guard 10."""

    def test_an_oversized_quantity_blocks(self, chain):
        result = chain.check_order(ctx(intent=make_intent(quantity=50_000)))
        assert not result.allowed and result.guard == "fat_finger"

    def test_a_zero_quantity_blocks(self, chain):
        assert not chain.check_order(ctx(intent=make_intent(quantity=0))).allowed

    def test_a_negative_quantity_blocks(self, chain):
        assert not chain.check_order(ctx(intent=make_intent(quantity=-65))).allowed

    def test_an_oversized_notional_blocks(self, chain):
        result = chain.check_order(
            ctx(intent=make_intent(quantity=1000), reference_price=500.0))
        assert not result.allowed and result.guard == "fat_finger"

    def test_a_normal_order_passes(self, chain):
        assert chain.check_order(
            ctx(intent=make_intent(quantity=65), reference_price=120.0)).allowed


class TestFunds:
    """Guard 8 — fails closed."""

    def test_insufficient_funds_block(self, chain):
        result = chain.check_order(ctx(intent=make_intent(quantity=65),
                                       reference_price=120.0,
                                       available_funds=100.0))
        assert not result.allowed and result.guard == "insufficient_funds"

    def test_unknown_balance_blocks(self, chain):
        """None means the balance call failed. Sending an order against an
        unknown balance risks a rejection that leaves the strategy believing it
        holds a position it does not."""
        result = chain.check_order(ctx(intent=make_intent(), reference_price=120.0,
                                       available_funds=None))
        assert not result.allowed and result.guard == "insufficient_funds"

    def test_an_exit_is_never_blocked_for_funds(self, chain):
        """Exits reduce exposure. Blocking one because margin looks short is
        exactly backwards — it traps the position."""
        sell = make_signal(action=Action.SELL)
        intent = make_intent(signal=sell)
        intent.transaction_type = TransactionType.SELL
        assert chain.check_order(ctx(intent=intent, reference_price=120.0,
                                     available_funds=0.0)).allowed


class TestSlippage:
    """Guard 13."""

    def test_a_far_off_limit_price_blocks(self, chain):
        result = chain.check_order(
            ctx(intent=make_intent(price=200.0, order_type=OrderType.LIMIT),
                reference_price=120.0))
        assert not result.allowed and result.guard == "slippage"

    def test_a_close_limit_price_passes(self, chain):
        assert chain.check_order(
            ctx(intent=make_intent(price=122.0, order_type=OrderType.LIMIT),
                reference_price=120.0)).allowed

    def test_a_market_order_is_exempt(self, chain):
        assert chain.check_order(
            ctx(intent=make_intent(price=0.0), reference_price=120.0)).allowed


class TestOrdering:
    """The chain stops at the first block, cheapest checks first."""

    def test_the_kill_switch_wins_over_everything_else(self, chain, switch):
        switch.trip("halted")
        result = chain.check_signal(ctx(last_tick_age=None, trades_today=99,
                                        realised_pnl=-99_999.0, reconciled=False))
        assert result.guard == "kill_switch"

    def test_every_block_is_journalled(self, chain, journal):
        chain.check_signal(ctx(last_tick_age=999.0))
        rows = list(journal.conn.execute(
            "SELECT guard, allowed FROM guard_events"))
        assert rows and rows[-1]["guard"] == "feed_staleness"
        assert rows[-1]["allowed"] == 0


class TestStalenessOnLongTimeframes:
    """A bar carries its OPEN time, so measuring staleness from it made every
    2-hour signal look 7200s old against a 120s limit — the guard blocked
    every trade the plan would ever make."""

    @staticmethod
    def _ctx(tf: str, minutes_after_close: float):
        import datetime as dt
        from orbot.config import SignalConfig, TradePlan
        from orbot.constants import ExchangeSegment, InstrumentType
        from orbot.execution.guards import GuardContext, IST_TZ
        from orbot.models import Action, Signal
        from orbot.engine.timeframe import Timeframe

        plan = TradePlan(name="p", signal=SignalConfig(
            exchange_segment=ExchangeSegment.MCX_COMM, security_id="1",
            instrument_type=InstrumentType.FUTCOM, timeframe=tf))
        bar = dt.datetime(2026, 8, 17, 9, 0, tzinfo=IST_TZ)
        span = dt.timedelta(minutes=Timeframe.parse(tf).minutes or 0)
        now = bar + span + dt.timedelta(minutes=minutes_after_close)
        sig = Signal(plan="p", action=Action.BUY, bar_ts=bar, price=1.0,
                     source="engine")
        return GuardContext(now=now, plan=plan, signal=sig)

    def test_fresh_two_hour_bar_is_allowed(self, chain):
        assert chain._signal_fresh(self._ctx("2h", 0.2)).allowed

    def test_stale_two_hour_bar_is_still_blocked(self, chain):
        got = chain._signal_fresh(self._ctx("2h", 30))
        assert not got.allowed and got.guard == "signal_staleness"

    def test_five_minute_bar_unaffected(self, chain):
        assert chain._signal_fresh(self._ctx("5m", 0.5)).allowed
        assert not chain._signal_fresh(self._ctx("5m", 10)).allowed
