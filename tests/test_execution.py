"""Resolver, position book and OMS tests.

The resolver's job is to fail loudly rather than return a *valid but wrong*
contract. A wrong strike, a stale expiry, or an index security id sent as
tradeable all produce orders that look entirely normal in the logs and in the
broker's order book. Most of these tests assert that something raises.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from orbot.broker.instruments import InstrumentError, InstrumentRegistry
from orbot.config import ExecutionConfig, LegConfig, SignalConfig, TradePlan
from orbot.constants import (
    ExchangeSegment,
    InstrumentType,
    OrderStatus,
    OrderType,
    ProductType,
    TransactionType,
)
from orbot.execution.oms import PaperOMS
from orbot.execution.positions import PositionBook
from orbot.execution.resolver import ExecutionResolver, ResolutionError, parse_strike_spec
from orbot.models import Action, Instrument, Order, OrderIntent, Position, Signal

IST = ZoneInfo("Asia/Kolkata")

HEADER = (
    "EXCH_ID,SEGMENT,SECURITY_ID,ISIN,INSTRUMENT,UNDERLYING_SECURITY_ID,UNDERLYING_SYMBOL,"
    "SYMBOL_NAME,DISPLAY_NAME,INSTRUMENT_TYPE,SERIES,LOT_SIZE,SM_EXPIRY_DATE,STRIKE_PRICE,"
    "OPTION_TYPE,TICK_SIZE,SM_FREEZE_QTY,"
)


def row(exch, seg, secid, instr, und, symbol, display, lot, expiry, strike, opt,
        series="NA", tick="0.05", freeze="1800") -> str:
    return (f"{exch},{seg},{secid},NA,{instr},0,{und},{symbol},{display},{instr},"
            f"{series},{lot},{expiry},{strike},{opt},{tick},{freeze},")


@pytest.fixture
def registry(tmp_path: Path) -> InstrumentRegistry:
    # Expiries are relative to today, never hardcoded. A fixed date is a time
    # bomb: these were 2026-08-20, and the suite went red on the 21st because
    # the contracts had expired -- a failure that says nothing about the code.
    weekly = (dt.date.today() + dt.timedelta(days=6)).isoformat()
    monthly = (dt.date.today() + dt.timedelta(days=13)).isoformat()
    lines = [
        HEADER,
        row("NSE", "I", "13", "INDEX", "NIFTY", "NIFTY", "Nifty 50", "1",
            "0001-01-01", "-0.01", "XX", freeze="0"),
        row("NSE", "E", "2885", "EQUITY", "RELIANCE", "RELIANCE", "RELIANCE",
            "1", "0001-01-01", "-0.01", "XX", series="EQ", freeze="0"),
        row("NSE", "D", "70001", "FUTIDX", "NIFTY", "NIFTY", "NIFTY AUG FUT",
            "65", monthly, "-0.01", "XX"),
    ]
    secid = 80000
    for strike in range(24_000, 25_001, 100):
        for opt in ("CE", "PE"):
            lines.append(row("NSE", "D", str(secid), "OPTIDX", "NIFTY", "NIFTY",
                             f"NIFTY {strike} {opt}", "65", weekly,
                             f"{strike}.00000", opt, tick="5.0"))
            secid += 1
    path = tmp_path / "s.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return InstrumentRegistry.load(path)


def option_plan(**exec_kwargs) -> TradePlan:
    cfg = dict(mode="option", underlying="NIFTY",
               on_buy=LegConfig(option_type="CE", strike="ATM", expiry="nearest"),
               product=ProductType.INTRADAY, order_type=OrderType.MARKET, qty_lots=1)
    cfg.update(exec_kwargs)
    return TradePlan(
        name="nifty-atm-ce",
        signal=SignalConfig(exchange_segment=ExchangeSegment.IDX_I,
                            security_id="13", timeframe="5m"),
        execution=ExecutionConfig(**cfg),
    )


def buy_signal(price: float = 24_287.0) -> Signal:
    return Signal(plan="nifty-atm-ce", action=Action.BUY,
                  bar_ts=dt.datetime(2026, 8, 14, 11, 0, tzinfo=IST), price=price)


TODAY = dt.date(2026, 8, 14)


# =====================================================================
class TestStrikeSpecParsing:
    @pytest.mark.parametrize("spec,expected", [
        ("ATM", 0), ("atm", 0), ("ITM1", 1), ("ITM3", 3),
        ("OTM1", -1), ("OTM2", -2), ("ATM+1", 1), ("ATM-2", -2),
    ])
    def test_offsets(self, spec, expected):
        assert parse_strike_spec(spec) == expected

    def test_bare_itm_and_otm_mean_one_step(self):
        assert parse_strike_spec("ITM") == 1
        assert parse_strike_spec("OTM") == -1

    def test_absolute_strike_returns_a_float(self):
        assert parse_strike_spec("24500") == 24500.0
        assert isinstance(parse_strike_spec("24500"), float)

    @pytest.mark.parametrize("spec", ["", "banana", "ATM++1", "-100", "0", "nan"])
    def test_garbage_raises(self, spec):
        with pytest.raises(ResolutionError):
            parse_strike_spec(spec)


class TestOptionResolution:
    def test_atm_call_resolves_to_the_nearest_strike(self, registry):
        resolver = ExecutionResolver(registry, option_plan())
        intent = resolver.resolve_entry(buy_signal(24_287.0), today=TODAY)
        assert intent.instrument.strike == 24_300.0
        assert intent.instrument.option_type.value == "CE"
        assert intent.quantity == 65             # 1 lot x 65

    def test_quantity_scales_with_lots(self, registry):
        resolver = ExecutionResolver(registry, option_plan(qty_lots=3))
        intent = resolver.resolve_entry(buy_signal(), today=TODAY)
        assert intent.quantity == 195

    def test_itm_offset_moves_the_strike_down_for_a_call(self, registry):
        plan = option_plan(on_buy=LegConfig(option_type="CE", strike="ITM2",
                                            expiry="nearest"))
        intent = ExecutionResolver(registry, plan).resolve_entry(
            buy_signal(24_500.0), today=TODAY)
        assert intent.instrument.strike == 24_300.0

    def test_an_absolute_strike_is_used_verbatim(self, registry):
        plan = option_plan(on_buy=LegConfig(option_type="CE", strike="24800",
                                            expiry="nearest"))
        intent = ExecutionResolver(registry, plan).resolve_entry(
            buy_signal(24_100.0), today=TODAY)
        assert intent.instrument.strike == 24_800.0

    def test_an_absolute_strike_that_does_not_exist_raises(self, registry):
        plan = option_plan(on_buy=LegConfig(option_type="CE", strike="24_050",
                                            expiry="nearest"))
        with pytest.raises((ResolutionError, InstrumentError)):
            ExecutionResolver(registry, plan).resolve_entry(buy_signal(), today=TODAY)

    def test_the_signal_price_is_used_when_no_spot_is_given(self, registry):
        resolver = ExecutionResolver(registry, option_plan())
        intent = resolver.resolve_entry(buy_signal(24_610.0), today=TODAY)
        assert intent.instrument.strike == 24_600.0

    def test_a_live_spot_overrides_the_signal_price(self, registry):
        """The signal price is the bar close; by the time the order goes out the
        market has moved, and the strike should follow the market."""
        resolver = ExecutionResolver(registry, option_plan())
        intent = resolver.resolve_entry(buy_signal(24_100.0), spot=24_900.0,
                                        today=TODAY)
        assert intent.instrument.strike == 24_900.0

    @pytest.mark.parametrize("spot", [0.0, -5.0, float("nan"), float("inf")])
    def test_an_unusable_spot_raises(self, registry, spot):
        resolver = ExecutionResolver(registry, option_plan())
        with pytest.raises(ResolutionError):
            resolver.resolve_entry(buy_signal(), spot=spot, today=TODAY)

    def test_a_sell_signal_is_refused_at_the_entry_path(self, registry):
        """SELL means exit for this strategy; routing one into resolve_entry
        would open a position on an exit signal."""
        resolver = ExecutionResolver(registry, option_plan())
        sell = Signal(plan="p", action=Action.SELL,
                      bar_ts=dt.datetime(2026, 8, 14, 11, 0, tzinfo=IST),
                      price=24_500.0)
        with pytest.raises(ResolutionError):
            resolver.resolve_entry(sell, today=TODAY)


class TestFutureResolution:
    def test_resolves_the_configured_expiry(self, registry):
        plan = option_plan(mode="future",
                           on_buy=LegConfig(expiry="nearest_monthly"))
        intent = ExecutionResolver(registry, plan).resolve_entry(
            buy_signal(), today=TODAY)
        assert intent.instrument.display_name == "NIFTY AUG FUT"
        assert intent.quantity == 65


class TestCashResolution:
    def test_mode_same_on_an_equity_trades_that_equity(self, registry):
        plan = TradePlan(
            name="rel",
            signal=SignalConfig(exchange_segment=ExchangeSegment.NSE_EQ,
                                security_id="2885", timeframe="15m"),
            execution=ExecutionConfig(mode="same", quantity=10),
        )
        intent = ExecutionResolver(registry, plan).resolve_entry(
            buy_signal(1400.0), today=TODAY)
        assert intent.instrument.security_id == "2885"
        assert intent.quantity == 10

    def test_mode_same_on_an_index_raises(self, registry):
        """An index is data-only. Sending its security id as tradeable is the
        single most plausible way to build an order that cannot ever fill."""
        plan = TradePlan(
            name="bad",
            signal=SignalConfig(exchange_segment=ExchangeSegment.IDX_I,
                                security_id="13", timeframe="5m"),
            execution=ExecutionConfig(mode="same", quantity=1),
        )
        with pytest.raises(ResolutionError):
            ExecutionResolver(registry, plan).resolve_entry(buy_signal(), today=TODAY)


class TestValidation:
    """Static checks, so a bad plan fails at 08:00 rather than at 09:20."""

    def test_a_good_option_plan_validates(self, registry):
        assert ExecutionResolver(registry, option_plan()).validate() == []

    def test_index_with_mode_same_is_rejected(self, registry):
        plan = TradePlan(
            name="bad",
            signal=SignalConfig(exchange_segment=ExchangeSegment.IDX_I,
                                security_id="13", timeframe="5m"),
            execution=ExecutionConfig(mode="same", quantity=1),
        )
        problems = ExecutionResolver(registry, plan).validate()
        assert problems and "index cannot be traded" in problems[0]

    def test_option_mode_without_an_underlying_is_rejected(self, registry):
        plan = option_plan(underlying="")
        assert ExecutionResolver(registry, plan).validate()

    def test_option_mode_without_an_option_type_is_rejected(self, registry):
        plan = option_plan(on_buy=LegConfig(strike="ATM", expiry="nearest"))
        assert ExecutionResolver(registry, plan).validate()

    def test_an_unknown_underlying_is_rejected(self, registry):
        plan = option_plan(underlying="NOSUCHINDEX")
        assert ExecutionResolver(registry, plan).validate()

    def test_a_bad_strike_spec_is_rejected(self, registry):
        plan = option_plan(on_buy=LegConfig(option_type="CE", strike="banana",
                                            expiry="nearest"))
        assert ExecutionResolver(registry, plan).validate()


class TestFreezeLimit:
    def test_an_order_above_the_freeze_quantity_raises(self, registry):
        """NSE rejects these outright. Better to refuse with a clear message
        than to have the exchange bounce it while the strategy believes it is
        in a position."""
        plan = option_plan(qty_lots=100)      # 6500 vs a 1800 freeze limit
        with pytest.raises(ResolutionError, match="freeze"):
            ExecutionResolver(registry, plan).resolve_entry(buy_signal(), today=TODAY)


class TestExitResolution:
    def test_exit_sizes_from_the_position_not_the_config(self, registry):
        """A partial fill leaves a different quantity than configured. Exiting
        the configured size would leave a residual or reverse into a short."""
        resolver = ExecutionResolver(registry, option_plan(qty_lots=5))
        held = Position(security_id="80006", segment=ExchangeSegment.NSE_FNO,
                        net_qty=32, avg_price=120.0)
        intent = resolver.resolve_exit(held)
        assert intent.quantity == 32
        assert intent.transaction_type is TransactionType.SELL

    def test_exiting_a_short_buys_back(self, registry):
        resolver = ExecutionResolver(registry, option_plan())
        held = Position(security_id="80006", segment=ExchangeSegment.NSE_FNO,
                        net_qty=-65, avg_price=120.0)
        intent = resolver.resolve_exit(held)
        assert intent.transaction_type is TransactionType.BUY
        assert intent.quantity == 65

    def test_exiting_a_flat_position_raises(self, registry):
        resolver = ExecutionResolver(registry, option_plan())
        flat = Position(security_id="80006", segment=ExchangeSegment.NSE_FNO, net_qty=0)
        with pytest.raises(ResolutionError):
            resolver.resolve_exit(flat)


# =====================================================================
OPT = Instrument(security_id="80006", segment=ExchangeSegment.NSE_FNO,
                 instrument_type=InstrumentType.OPTIDX, symbol="NIFTY",
                 display_name="NIFTY 20 AUG 24300 CE", lot_size=65, tick_size=0.05)


def intent_for(side: TransactionType, qty: int) -> OrderIntent:
    return OrderIntent(plan="p", instrument=OPT, transaction_type=side,
                       quantity=qty, product_type=ProductType.INTRADAY,
                       order_type=OrderType.MARKET, correlation_id=f"c-{side}-{qty}")


def filled(intent: OrderIntent, price: float) -> Order:
    """Mirrors what the client returns: the order carries its intent's
    correlation id, which is how the book tells two order attempts apart."""
    return Order(order_id=f"o{price}", status=OrderStatus.TRADED,
                 security_id=OPT.security_id, segment=OPT.segment,
                 transaction_type=intent.transaction_type,
                 quantity=intent.quantity, filled_qty=intent.quantity,
                 avg_price=price, correlation_id=intent.correlation_id)


class TestPositionBook:
    def test_a_buy_opens_a_position(self):
        book = PositionBook()
        intent = intent_for(TransactionType.BUY, 65)
        book.apply_fill(intent, filled(intent, 120.0))
        position = book.get(OPT.key)
        assert position.net_qty == 65 and position.avg_price == 120.0

    def test_adding_averages_the_entry_price(self):
        book = PositionBook()
        for price in (100.0, 140.0):
            intent = intent_for(TransactionType.BUY, 65)
            intent.correlation_id = f"c{price}"
            book.apply_fill(intent, filled(intent, price))
        assert book.get(OPT.key).avg_price == pytest.approx(120.0)

    def test_two_orders_at_the_same_price_are_tracked_separately(self):
        """Distinct order attempts must not collide in the applied-quantity
        ledger, even when every visible field matches."""
        book = PositionBook()
        for i in range(2):
            intent = intent_for(TransactionType.BUY, 65)
            intent.correlation_id = f"unique-{i}"
            book.apply_fill(intent, filled(intent, 100.0))
        assert book.get(OPT.key).net_qty == 130

    def test_closing_realises_the_pnl_and_goes_flat(self):
        book = PositionBook()
        entry = intent_for(TransactionType.BUY, 65)
        book.apply_fill(entry, filled(entry, 100.0))
        exit_ = intent_for(TransactionType.SELL, 65)
        book.apply_fill(exit_, filled(exit_, 130.0))
        position = book.get(OPT.key)
        assert position.net_qty == 0
        assert position.realised == pytest.approx(65 * 30)
        assert book.open_positions() == []

    def test_a_loss_is_realised_negative(self):
        book = PositionBook()
        entry = intent_for(TransactionType.BUY, 65)
        book.apply_fill(entry, filled(entry, 130.0))
        exit_ = intent_for(TransactionType.SELL, 65)
        book.apply_fill(exit_, filled(exit_, 100.0))
        assert book.get(OPT.key).realised == pytest.approx(-65 * 30)

    def test_repeated_polls_of_the_same_order_do_not_double_count(self):
        """A poll loop reporting the same cumulative fill twice must not double
        the position."""
        book = PositionBook()
        intent = intent_for(TransactionType.BUY, 65)
        order = filled(intent, 120.0)
        for _ in range(4):
            book.apply_fill(intent, order)
        assert book.get(OPT.key).net_qty == 65

    def test_a_growing_partial_fill_applies_only_the_delta(self):
        book = PositionBook()
        intent = intent_for(TransactionType.BUY, 65)
        order = Order(order_id="o1", status=OrderStatus.PART_TRADED,
                      security_id=OPT.security_id, segment=OPT.segment,
                      transaction_type=TransactionType.BUY, quantity=65,
                      filled_qty=20, avg_price=120.0)
        book.apply_fill(intent, order)
        assert book.get(OPT.key).net_qty == 20
        order.filled_qty = 65
        order.status = OrderStatus.TRADED
        book.apply_fill(intent, order)
        assert book.get(OPT.key).net_qty == 65

    def test_marking_updates_unrealised(self):
        book = PositionBook()
        intent = intent_for(TransactionType.BUY, 65)
        book.apply_fill(intent, filled(intent, 100.0))
        book.mark(OPT.key, 110.0)
        assert book.unrealised == pytest.approx(650.0)


class TestReconciliation:
    def test_matching_books_reconcile(self):
        book = PositionBook()
        intent = intent_for(TransactionType.BUY, 65)
        book.apply_fill(intent, filled(intent, 120.0))
        broker = [Position(security_id=OPT.security_id, segment=OPT.segment,
                           net_qty=65, avg_price=120.0)]
        assert book.reconcile(broker).matched

    def test_a_quantity_difference_is_a_mismatch(self):
        book = PositionBook()
        intent = intent_for(TransactionType.BUY, 65)
        book.apply_fill(intent, filled(intent, 120.0))
        broker = [Position(security_id=OPT.security_id, segment=OPT.segment,
                           net_qty=130)]
        result = book.reconcile(broker)
        assert not result.matched
        assert "local +65" in result.detail and "broker +130" in result.detail

    def test_a_position_the_bot_knows_nothing_about_is_a_mismatch(self):
        """A manual trade in the Dhan app shows up here."""
        broker = [Position(security_id="99999", segment=ExchangeSegment.NSE_FNO,
                           net_qty=50)]
        assert not PositionBook().reconcile(broker).matched

    def test_a_position_the_broker_closed_is_a_mismatch(self):
        """Broker auto-square-off near the close."""
        book = PositionBook()
        intent = intent_for(TransactionType.BUY, 65)
        book.apply_fill(intent, filled(intent, 120.0))
        assert not book.reconcile([]).matched

    def test_flat_instruments_are_not_compared(self):
        book = PositionBook()
        entry = intent_for(TransactionType.BUY, 65)
        book.apply_fill(entry, filled(entry, 100.0))
        exit_ = intent_for(TransactionType.SELL, 65)
        book.apply_fill(exit_, filled(exit_, 100.0))
        assert book.reconcile([]).matched

    def test_adopt_replaces_the_local_view(self):
        book = PositionBook()
        broker = [Position(security_id="55555", segment=ExchangeSegment.NSE_FNO,
                           net_qty=100, avg_price=50.0)]
        book.adopt(broker)
        assert len(book) == 1
        assert book.reconcile(broker).matched


class TestPaperOMS:
    def test_a_buy_fills_immediately_and_in_full(self):
        oms = PaperOMS()
        intent = intent_for(TransactionType.BUY, 65)
        order = oms.place(intent, reference_price=120.0)
        assert order.status is OrderStatus.TRADED
        assert order.filled_qty == 65

    def test_buy_slippage_is_adverse(self):
        oms = PaperOMS(slippage_pct=1.0)
        order = oms.place(intent_for(TransactionType.BUY, 65), reference_price=100.0)
        assert order.avg_price > 100.0

    def test_sell_slippage_is_adverse(self):
        oms = PaperOMS(slippage_pct=1.0)
        order = oms.place(intent_for(TransactionType.SELL, 65), reference_price=100.0)
        assert order.avg_price < 100.0

    def test_fills_snap_to_the_tick_size(self):
        oms = PaperOMS(slippage_pct=0.37)
        order = oms.place(intent_for(TransactionType.BUY, 65), reference_price=123.456)
        assert abs(round(order.avg_price / OPT.tick_size) * OPT.tick_size
                   - order.avg_price) < 1e-9

    def test_no_reference_price_raises_rather_than_inventing_one(self):
        """Inventing a fill price would make paper P&L meaningless, which is
        worse than refusing."""
        oms = PaperOMS()
        with pytest.raises(ValueError):
            oms.place(intent_for(TransactionType.BUY, 65))

    def test_funds_decrease_on_a_buy_and_recover_on_a_sell(self):
        oms = PaperOMS(starting_funds=100_000.0, slippage_pct=0.0, slippage_ticks=0)
        oms.place(intent_for(TransactionType.BUY, 65), reference_price=100.0)
        assert oms.available_funds() == pytest.approx(100_000 - 6_500)
        oms.place(intent_for(TransactionType.SELL, 65), reference_price=100.0)
        assert oms.available_funds() == pytest.approx(100_000)

    def test_positions_track_and_flatten(self):
        oms = PaperOMS(slippage_pct=0.0, slippage_ticks=0)
        oms.place(intent_for(TransactionType.BUY, 65), reference_price=100.0)
        assert len(oms.positions()) == 1
        oms.place(intent_for(TransactionType.SELL, 65), reference_price=120.0)
        assert oms.positions() == []

    def test_realised_pnl_is_recorded_on_the_round_trip(self):
        oms = PaperOMS(slippage_pct=0.0, slippage_ticks=0)
        oms.place(intent_for(TransactionType.BUY, 65), reference_price=100.0)
        oms.place(intent_for(TransactionType.SELL, 65), reference_price=120.0)
        assert oms._positions[OPT.key].realised == pytest.approx(65 * 20)

    def test_settle_returns_immediately_on_a_terminal_order(self):
        oms = PaperOMS()
        order = oms.place(intent_for(TransactionType.BUY, 65), reference_price=120.0)
        assert oms.settle(order, timeout=0.1).status.is_terminal


class TestPaperFundsByProduct:
    """Buying a future blocks margin; buying a call costs the whole premium.
    Charging notional for both drove a Rs 3 lakh paper account to minus
    Rs 1.5 crore the first time it bought a gold future."""

    @staticmethod
    def _future_intent(side, qty):
        from orbot.constants import (ExchangeSegment, InstrumentType, OrderType,
                                     ProductType)
        from orbot.models import Instrument, OrderIntent
        inst = Instrument(security_id="563946", segment=ExchangeSegment.MCX_COMM,
                          symbol="GOLDM", display_name="GOLDM04SEP26FUT",
                          instrument_type=InstrumentType.FUTCOM, lot_size=100)
        return OrderIntent(instrument=inst, transaction_type=side, quantity=qty,
                           order_type=OrderType.MARKET,
                           product_type=ProductType.MARGIN, plan="goldm-2h")

    def test_a_future_blocks_margin_not_notional(self):
        from orbot.constants import TransactionType
        from orbot.execution.oms import PaperOMS

        oms = PaperOMS(starting_funds=300_000.0, slippage_pct=0.0, slippage_ticks=0,
                       margin_pct=0.11)
        oms.place(self._future_intent(TransactionType.BUY, 100),
                  reference_price=153_327.0)
        # GOLDM is 100 g quoted per 10 g, so the contract is Rs 15.3 lakh and
        # margin is 11% of that -- not 11% of a fictional Rs 15.3 crore.
        assert oms.available_funds() == pytest.approx(
            300_000 - 153_327 * 100 * 0.1 * 0.11)
        assert oms.available_funds() > 0

    def test_margin_is_released_and_pnl_realised_on_exit(self):
        from orbot.constants import TransactionType
        from orbot.execution.oms import PaperOMS

        oms = PaperOMS(starting_funds=300_000.0, slippage_pct=0.0, slippage_ticks=0)
        oms.place(self._future_intent(TransactionType.BUY, 100),
                  reference_price=150_000.0)
        oms.place(self._future_intent(TransactionType.SELL, 100),
                  reference_price=150_100.0)
        assert oms.positions() == []
        # 100 points at Rs 10 a point = Rs 1,000, and every rupee of margin back
        assert oms.available_funds() == pytest.approx(301_000.0)


class TestPositionBookMultiplier:
    """Mark-to-market must go through the contract multiplier, or the header
    P&L reads ten times too big on MCX gold."""

    @staticmethod
    def _intent(symbol, qty):
        from orbot.constants import (ExchangeSegment, InstrumentType, OrderType,
                                     ProductType, TransactionType)
        from orbot.models import Instrument, OrderIntent
        inst = Instrument(security_id="1", segment=ExchangeSegment.MCX_COMM,
                          symbol=symbol, display_name=symbol,
                          instrument_type=InstrumentType.FUTCOM, lot_size=100)
        return OrderIntent(instrument=inst,
                           transaction_type=TransactionType.BUY, quantity=qty,
                           order_type=OrderType.MARKET,
                           product_type=ProductType.MARGIN, plan="p")

    @staticmethod
    def _order(qty, price):
        from orbot.constants import OrderStatus
        from orbot.models import Order
        return Order(order_id="o1", status=OrderStatus.TRADED,
                     filled_qty=qty, avg_price=price)

    def test_gold_marks_at_ten_rupees_a_point(self):
        from orbot.execution.positions import PositionBook
        book = PositionBook()
        intent = self._intent("GOLDM", 100)
        book.apply_fill(intent, self._order(100, 153_327.0))
        book.mark(intent.instrument.key, 154_327.0)      # +1,000 points
        assert book.unrealised == pytest.approx(1_000 * 100 * 0.1)   # Rs 10,000

    def test_silver_marks_one_for_one(self):
        from orbot.execution.positions import PositionBook
        book = PositionBook()
        intent = self._intent("SILVER100", 200)
        book.apply_fill(intent, self._order(200, 2_378.0))
        book.mark(intent.instrument.key, 2_388.0)        # +10 points
        assert book.unrealised == pytest.approx(10 * 200)            # Rs 2,000
