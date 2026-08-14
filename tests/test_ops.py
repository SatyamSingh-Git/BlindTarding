"""Calendar, journal and kill switch tests.

The journal tests matter more than they look: it is the only durable record of
what the bot believed, and it is where deduplication actually lives. An
in-memory dedupe set forgets on restart, which turns a crash-loop into a
repeat-order loop.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from orbot.constants import ExchangeSegment, InstrumentType, OrderStatus, OrderType
from orbot.constants import ProductType, TransactionType
from orbot.models import Action, Instrument, Order, OrderIntent, Position, Signal
from orbot.ops.journal import DuplicateSignal, Journal
from orbot.ops.killswitch import KillSwitch
from orbot.ops.market_calendar import MarketCalendar

IST = ZoneInfo("Asia/Kolkata")

INSTRUMENT = Instrument(
    security_id="45102", segment=ExchangeSegment.NSE_FNO,
    instrument_type=InstrumentType.OPTIDX, symbol="NIFTY",
    display_name="NIFTY 20 AUG 24300 CE", lot_size=65,
)


@pytest.fixture
def journal(tmp_path: Path) -> Journal:
    j = Journal(tmp_path / "j.db")
    j.start_run("paper")
    yield j
    j.close()


def signal(action: str = Action.BUY, minute: int = 0) -> Signal:
    return Signal(plan="p", action=action,
                  bar_ts=dt.datetime(2026, 8, 14, 11, minute, tzinfo=IST),
                  price=24_500.0)


def intent(corr: str = "c1") -> OrderIntent:
    return OrderIntent(plan="p", instrument=INSTRUMENT,
                       transaction_type=TransactionType.BUY, quantity=65,
                       product_type=ProductType.INTRADAY,
                       order_type=OrderType.MARKET, correlation_id=corr)


# =====================================================================
class TestCalendar:
    def test_a_normal_weekday_session_is_open(self):
        cal = MarketCalendar()
        assert cal.is_open(dt.datetime(2026, 8, 14, 11, 0, tzinfo=IST))

    @pytest.mark.parametrize("when", [
        dt.datetime(2026, 8, 14, 9, 14, tzinfo=IST),    # one minute early
        dt.datetime(2026, 8, 14, 15, 30, tzinfo=IST),   # close is exclusive
        dt.datetime(2026, 8, 15, 11, 0, tzinfo=IST),    # Saturday
        dt.datetime(2026, 8, 16, 11, 0, tzinfo=IST),    # Sunday
    ])
    def test_closed_times(self, when):
        assert not MarketCalendar().is_open(when)

    def test_the_open_boundary_is_inclusive(self):
        assert MarketCalendar().is_open(dt.datetime(2026, 8, 14, 9, 15, tzinfo=IST))

    def test_preopen_is_not_open(self):
        """Prices tick during the auction, but they are auction prices —
        signalling off them would be wrong."""
        cal = MarketCalendar()
        when = dt.datetime(2026, 8, 14, 9, 5, tzinfo=IST)
        assert cal.is_preopen(when)
        assert not cal.is_open(when)

    def test_a_holiday_closes_the_day(self):
        cal = MarketCalendar(holidays={dt.date(2026, 8, 14)})
        assert not cal.is_trading_day(dt.date(2026, 8, 14))

    def test_a_special_session_overrides_a_weekend(self):
        cal = MarketCalendar(special_sessions={
            dt.date(2026, 8, 15): (dt.time(18, 15), dt.time(19, 15))})
        assert cal.is_trading_day(dt.date(2026, 8, 15))
        assert cal.is_open(dt.datetime(2026, 8, 15, 18, 30, tzinfo=IST))
        assert not cal.is_open(dt.datetime(2026, 8, 15, 11, 0, tzinfo=IST))

    def test_minutes_to_close(self):
        cal = MarketCalendar()
        when = dt.datetime(2026, 8, 14, 15, 0, tzinfo=IST)
        assert cal.minutes_to_close(when) == pytest.approx(30.0)

    def test_minutes_to_close_goes_negative_after_the_bell(self):
        cal = MarketCalendar()
        assert cal.minutes_to_close(dt.datetime(2026, 8, 14, 16, 0, tzinfo=IST)) < 0

    def test_next_trading_day_skips_the_weekend(self):
        cal = MarketCalendar()
        assert cal.next_trading_day(dt.date(2026, 8, 14)) == dt.date(2026, 8, 17)

    def test_next_trading_day_skips_holidays_too(self):
        cal = MarketCalendar(holidays={dt.date(2026, 8, 17)})
        assert cal.next_trading_day(dt.date(2026, 8, 14)) == dt.date(2026, 8, 18)

    def test_naive_datetimes_are_localised(self):
        cal = MarketCalendar()
        assert cal.is_open(dt.datetime(2026, 8, 14, 11, 0))

    def test_an_empty_config_defaults_to_no_holidays(self):
        """Defaulting to empty makes the system lean on the feed-staleness
        guard, which is the safe failure mode. A stale list claiming 'open' on a
        closed day is not."""
        cal = MarketCalendar.load(None)
        assert cal.holidays == set()

    def test_loading_a_config(self, tmp_path):
        path = tmp_path / "cal.yaml"
        path.write_text(
            "open: '09:15'\nclose: '15:30'\nholidays:\n  - 2026-08-14\n"
            "special_sessions:\n  - date: 2026-11-08\n    open: '18:15'\n"
            "    close: '19:15'\n", encoding="utf-8")
        cal = MarketCalendar.load(path)
        assert dt.date(2026, 8, 14) in cal.holidays
        assert cal.session_window(dt.date(2026, 11, 8)) == (dt.time(18, 15),
                                                            dt.time(19, 15))

    def test_a_malformed_date_is_skipped_not_fatal(self, tmp_path):
        path = tmp_path / "cal.yaml"
        path.write_text("holidays:\n  - not-a-date\n  - 2026-08-14\n", encoding="utf-8")
        cal = MarketCalendar.load(path)
        assert cal.holidays == {dt.date(2026, 8, 14)}

    def test_describe_is_human_readable(self):
        cal = MarketCalendar()
        assert "open" in cal.describe(dt.datetime(2026, 8, 14, 11, 0, tzinfo=IST))
        assert "weekend" in cal.describe(dt.datetime(2026, 8, 15, 11, 0, tzinfo=IST))


class TestJournalDedupe:
    def test_a_signal_records_once(self, journal):
        assert journal.record_signal(signal()) > 0

    def test_the_same_signal_twice_raises(self, journal):
        journal.record_signal(signal())
        with pytest.raises(DuplicateSignal):
            journal.record_signal(signal())

    def test_a_different_bar_is_not_a_duplicate(self, journal):
        journal.record_signal(signal(minute=0))
        assert journal.record_signal(signal(minute=5)) > 0

    def test_the_opposite_action_on_the_same_bar_is_not_a_duplicate(self, journal):
        journal.record_signal(signal(Action.BUY))
        assert journal.record_signal(signal(Action.SELL)) > 0

    def test_has_signal_reflects_what_was_recorded(self, journal):
        sig = signal()
        assert not journal.has_signal(sig.dedupe_key)
        journal.record_signal(sig)
        assert journal.has_signal(sig.dedupe_key)

    def test_dedupe_survives_reopening_the_database(self, tmp_path):
        """The restart case, which is the whole reason this lives in SQLite."""
        path = tmp_path / "j.db"
        first = Journal(path)
        first.start_run("paper")
        first.record_signal(signal())
        first.close()

        second = Journal(path)
        try:
            assert second.has_signal(signal().dedupe_key)
            with pytest.raises(DuplicateSignal):
                second.record_signal(signal())
        finally:
            second.close()


class TestJournalOrders:
    def test_recording_and_updating_an_order(self, journal):
        order = Order(order_id="o1", status=OrderStatus.PENDING,
                      correlation_id="c1", quantity=65)
        journal.record_order(intent(), order, mode="paper")
        assert len(journal.open_orders()) == 1

        order.status = OrderStatus.TRADED
        order.filled_qty = 65
        order.avg_price = 120.0
        journal.update_order(order)
        assert journal.open_orders() == []

    def test_recording_the_same_correlation_id_upserts(self, journal):
        """Placement and settlement both write; the second must update rather
        than create a phantom second order."""
        order = Order(order_id="o1", status=OrderStatus.PENDING, correlation_id="c1")
        journal.record_order(intent("c1"), order, mode="paper")
        order.status = OrderStatus.TRADED
        journal.record_order(intent("c1"), order, mode="paper")
        rows = list(journal.conn.execute("SELECT * FROM orders"))
        assert len(rows) == 1 and rows[0]["status"] == "TRADED"

    def test_trades_today_counts_only_today(self, journal):
        order = Order(order_id="o1", status=OrderStatus.TRADED, correlation_id="c1")
        journal.record_order(intent("c1"), order, mode="paper")
        assert journal.trades_today() == 1
        assert journal.trades_today(day=dt.date(2020, 1, 1)) == 0

    def test_rejected_orders_are_not_counted_as_trades(self, journal):
        order = Order(order_id="o1", status=OrderStatus.REJECTED, correlation_id="c1")
        journal.record_order(intent("c1"), order, mode="paper")
        assert journal.trades_today() == 0


class TestJournalPnl:
    def test_daily_pnl_round_trips(self, journal):
        journal.update_daily_pnl(1500.0, -200.0)
        assert journal.daily_pnl() == (1500.0, -200.0)

    def test_updating_replaces_rather_than_appends(self, journal):
        journal.update_daily_pnl(100.0, 0.0)
        journal.update_daily_pnl(250.0, 0.0)
        assert journal.daily_pnl()[0] == 250.0

    def test_a_halt_flag_is_sticky(self, journal):
        """Once halted, a later non-halted update must not silently un-halt the
        day — a limit that resets itself is not a limit."""
        journal.update_daily_pnl(-6000.0, 0.0, halted=True)
        journal.update_daily_pnl(-100.0, 0.0, halted=False)
        assert journal.was_halted_today()

    def test_an_unrecorded_day_is_flat(self, journal):
        assert journal.daily_pnl(dt.date(2020, 1, 1)) == (0.0, 0.0)

    def test_position_snapshots_are_stored(self, journal):
        journal.snapshot_positions(
            [Position(security_id="45102", segment=ExchangeSegment.NSE_FNO,
                      net_qty=65, avg_price=120.0)], source="broker")
        rows = list(journal.conn.execute("SELECT * FROM position_snapshots"))
        assert len(rows) == 1 and rows[0]["net_qty"] == 65


class TestJournalGuards:
    def test_guard_events_are_recorded(self, journal):
        journal.record_guard("feed_staleness", False, "last tick 200s ago", plan="p")
        rows = list(journal.conn.execute("SELECT * FROM guard_events"))
        assert len(rows) == 1
        assert rows[0]["guard"] == "feed_staleness" and rows[0]["allowed"] == 0


class TestKillSwitch:
    def test_starts_clear(self, tmp_path):
        assert not KillSwitch(tmp_path / "HALT").is_halted

    def test_tripping_halts_and_writes_the_file(self, tmp_path):
        path = tmp_path / "HALT"
        switch = KillSwitch(path)
        switch.trip("daily loss")
        assert switch.is_halted and path.exists()
        assert "daily loss" in path.read_text(encoding="utf-8")

    def test_a_trip_survives_a_new_instance(self, tmp_path):
        path = tmp_path / "HALT"
        KillSwitch(path).trip("loss cap")
        assert KillSwitch(path).is_halted

    def test_non_persistent_trip_does_not_write_a_file(self, tmp_path):
        path = tmp_path / "HALT"
        switch = KillSwitch(path)
        switch.trip("transient", persist=False)
        assert switch.is_halted and not path.exists()
        assert not KillSwitch(path).is_halted

    def test_clearing_removes_the_file_and_the_state(self, tmp_path):
        path = tmp_path / "HALT"
        switch = KillSwitch(path)
        switch.trip("x")
        switch.clear()
        assert not switch.is_halted and not path.exists()

    def test_the_first_reason_is_kept(self, tmp_path):
        """The first failure is the diagnostic one; later trips are usually
        consequences of it."""
        switch = KillSwitch(tmp_path / "HALT")
        switch.trip("original cause")
        switch.trip("downstream effect")
        assert switch.state().reason == "original cause"

    def test_an_externally_created_file_halts(self, tmp_path):
        path = tmp_path / "HALT"
        switch = KillSwitch(path)
        assert not switch.is_halted
        path.write_text("stopped from another terminal", encoding="utf-8")
        assert switch.is_halted
        assert "another terminal" in switch.state().reason

    def test_state_reports_its_source(self, tmp_path):
        path = tmp_path / "HALT"
        path.write_text("external", encoding="utf-8")
        assert KillSwitch(path).state().source == "file"
        switch = KillSwitch(tmp_path / "OTHER")
        switch.trip("internal", persist=False)
        assert switch.state().source == "in-process"
