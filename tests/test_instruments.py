"""Instrument registry tests.

Strike and expiry selection decide what actually gets bought. A wrong strike is
a wrong trade that still looks entirely successful in the logs, so these are
asserted against a hand-built chain rather than live data.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from orbot.broker.instruments import InstrumentError, InstrumentRegistry, _monthly_expiries
from orbot.constants import ExchangeSegment, InstrumentType, OptionType

HEADER = (
    "EXCH_ID,SEGMENT,SECURITY_ID,ISIN,INSTRUMENT,UNDERLYING_SECURITY_ID,UNDERLYING_SYMBOL,"
    "SYMBOL_NAME,DISPLAY_NAME,INSTRUMENT_TYPE,SERIES,LOT_SIZE,SM_EXPIRY_DATE,STRIKE_PRICE,"
    "OPTION_TYPE,TICK_SIZE,EXPIRY_FLAG,BRACKET_FLAG,COVER_FLAG,ASM_GSM_FLAG,ASM_GSM_CATEGORY,"
    "BUY_SELL_INDICATOR,BUY_CO_MIN_MARGIN_PER,BUY_CO_SL_RANGE_MAX_PERC,"
    "BUY_CO_SL_RANGE_MIN_PERC,BUY_BO_MIN_MARGIN_PER,BUY_BO_PROFIT_RANGE_MAX_PERC,"
    "BUY_BO_PROFIT_RANGE_MIN_PERC,MTF_LEVERAGE,SM_UPPER_LIMIT,SM_LOWER_LIMIT,SM_FREEZE_QTY,"
)

TAIL = "M,N,N,N,NA,A,0,0,0,0,0,0,0,0,0,1800,"


def row(exch, seg, secid, instr, und, symbol, display, lot, expiry, strike, opt,
        series="NA", tick="0.05") -> str:
    return (f"{exch},{seg},{secid},NA,{instr},0,{und},{symbol},{display},{instr},"
            f"{series},{lot},{expiry},{strike},{opt},{tick},{TAIL}")


@pytest.fixture
def registry(tmp_path: Path) -> InstrumentRegistry:
    lines = [HEADER,
             row("NSE", "I", "13", "INDEX", "NIFTY", "NIFTY", "Nifty 50", "1", "NA", "-0.01", "XX"),
             row("NSE", "I", "25", "INDEX", "BANKNIFTY", "BANKNIFTY", "Nifty Bank", "1",
                 "NA", "-0.01", "XX"),
             row("NSE", "E", "2885", "EQUITY", "RELIANCE", "RELIANCE", "RELIANCE",
                 "1", "NA", "-0.01", "XX", series="EQ"),
             row("NSE", "E", "2886", "EQUITY", "RELIANCE", "RELIANCE", "RELIANCE BE",
                 "1", "NA", "-0.01", "XX", series="BE"),
             row("NSE", "D", "70001", "FUTIDX", "NIFTY", "NIFTY", "NIFTY AUG FUT",
                 "65", "2026-08-27", "-0.01", "XX"),
             row("NSE", "D", "70002", "FUTIDX", "NIFTY", "NIFTY", "NIFTY SEP FUT",
                 "65", "2026-09-24", "-0.01", "XX")]

    # Two expiries, strikes 24000..25000 in steps of 100, both CE and PE.
    secid = 80000
    for expiry in ("2026-08-20", "2026-08-27"):
        for strike in range(24_000, 25_001, 100):
            for opt in ("CE", "PE"):
                label = "CALL" if opt == "CE" else "PUT"
                lines.append(row(
                    "NSE", "D", str(secid), "OPTIDX", "NIFTY", "NIFTY",
                    f"NIFTY {expiry} {strike} {label}", "65", expiry,
                    f"{strike}.00000", opt, tick="5.0"))
                secid += 1

    path = tmp_path / "scrip.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return InstrumentRegistry.load(path)


class TestParsing:
    def test_segments_map_from_exch_and_segment_columns(self, registry):
        assert registry.index("NIFTY").segment is ExchangeSegment.IDX_I
        assert registry.equity("RELIANCE").segment is ExchangeSegment.NSE_EQ
        assert registry.get(ExchangeSegment.NSE_FNO, "70001").segment is ExchangeSegment.NSE_FNO

    def test_index_lookup(self, registry):
        nifty = registry.index("NIFTY")
        assert nifty.security_id == "13"
        assert nifty.instrument_type is InstrumentType.INDEX

    def test_equity_prefers_the_eq_series(self, registry):
        """BE/BZ are surveillance series — picking one silently changes the
        instrument you trade."""
        assert registry.equity("RELIANCE").security_id == "2885"

    def test_non_option_rows_have_no_strike(self, registry):
        assert registry.index("NIFTY").strike is None
        assert registry.index("NIFTY").option_type is None

    def test_option_rows_carry_strike_expiry_and_lot(self, registry):
        opt = registry.option("NIFTY", OptionType.CE, 24_500.0, dt.date(2026, 8, 20))
        assert opt.strike == 24_500.0
        assert opt.expiry == dt.date(2026, 8, 20)
        assert opt.lot_size == 65
        assert opt.option_type is OptionType.CE

    def test_unknown_instrument_raises_rather_than_guessing(self, registry):
        with pytest.raises(InstrumentError):
            registry.get(ExchangeSegment.NSE_EQ, "999999")


class TestExpirySelection:
    def test_nearest(self, registry):
        chosen = registry.select_expiry("NIFTY", InstrumentType.OPTIDX, "nearest",
                                        today=dt.date(2026, 8, 14))
        assert chosen == dt.date(2026, 8, 20)

    def test_next(self, registry):
        chosen = registry.select_expiry("NIFTY", InstrumentType.OPTIDX, "next",
                                        today=dt.date(2026, 8, 14))
        assert chosen == dt.date(2026, 8, 27)

    def test_expired_contracts_are_excluded(self, registry):
        chosen = registry.select_expiry("NIFTY", InstrumentType.OPTIDX, "nearest",
                                        today=dt.date(2026, 8, 21))
        assert chosen == dt.date(2026, 8, 27)

    def test_explicit_date(self, registry):
        chosen = registry.select_expiry("NIFTY", InstrumentType.OPTIDX, "2026-08-27",
                                        today=dt.date(2026, 8, 14))
        assert chosen == dt.date(2026, 8, 27)

    def test_monthly_is_the_last_expiry_of_the_month(self):
        expiries = [dt.date(2026, 8, 6), dt.date(2026, 8, 13), dt.date(2026, 8, 27),
                    dt.date(2026, 9, 3), dt.date(2026, 9, 24)]
        assert _monthly_expiries(expiries) == [dt.date(2026, 8, 27), dt.date(2026, 9, 24)]

    def test_no_live_expiry_raises(self, registry):
        with pytest.raises(InstrumentError):
            registry.select_expiry("NIFTY", InstrumentType.OPTIDX, "nearest",
                                   today=dt.date(2027, 1, 1))


class TestStrikeSelection:
    EXPIRY = dt.date(2026, 8, 20)

    def test_atm_picks_the_nearest_strike(self, registry):
        opt = registry.select_option("NIFTY", OptionType.CE, 24_512.0, self.EXPIRY)
        assert opt.strike == 24_500.0

    def test_atm_rounds_to_the_closer_strike(self, registry):
        assert registry.select_option("NIFTY", OptionType.CE, 24_561.0,
                                      self.EXPIRY).strike == 24_600.0
        assert registry.select_option("NIFTY", OptionType.CE, 24_549.0,
                                      self.EXPIRY).strike == 24_500.0

    def test_positive_offset_is_itm_for_a_call(self, registry):
        """A call goes in-the-money as the strike falls."""
        opt = registry.select_option("NIFTY", OptionType.CE, 24_500.0, self.EXPIRY, offset=1)
        assert opt.strike == 24_400.0

    def test_positive_offset_is_itm_for_a_put_too(self, registry):
        """Sign is mirrored so `ITM1` means the same thing in config for CE and PE."""
        opt = registry.select_option("NIFTY", OptionType.PE, 24_500.0, self.EXPIRY, offset=1)
        assert opt.strike == 24_600.0

    def test_negative_offset_is_otm_for_a_call(self, registry):
        opt = registry.select_option("NIFTY", OptionType.CE, 24_500.0, self.EXPIRY, offset=-2)
        assert opt.strike == 24_700.0

    def test_offset_clamps_at_the_edge_of_the_chain(self, registry):
        opt = registry.select_option("NIFTY", OptionType.CE, 24_500.0, self.EXPIRY, offset=99)
        assert opt.strike == 24_000.0

    def test_spot_outside_the_chain_clamps(self, registry):
        assert registry.select_option("NIFTY", OptionType.CE, 99_999.0,
                                      self.EXPIRY).strike == 25_000.0

    def test_strike_ladder_is_sorted_and_complete(self, registry):
        ladder = registry.strikes("NIFTY", InstrumentType.OPTIDX, self.EXPIRY, OptionType.CE)
        assert ladder == sorted(ladder)
        assert len(ladder) == 11


class TestFutures:
    def test_lookup_by_expiry(self, registry):
        fut = registry.future("NIFTY", dt.date(2026, 8, 27))
        assert fut.security_id == "70001"
        assert fut.lot_size == 65

    def test_expiry_rules_apply_to_futures(self, registry):
        chosen = registry.select_expiry("NIFTY", InstrumentType.FUTIDX, "nearest",
                                        today=dt.date(2026, 8, 14))
        assert registry.future("NIFTY", chosen).display_name == "NIFTY AUG FUT"


class TestLoadFilters:
    def test_underlying_filter_drops_other_chains(self, tmp_path):
        lines = [HEADER,
                 row("NSE", "I", "13", "INDEX", "NIFTY", "NIFTY", "Nifty 50", "1",
                     "NA", "-0.01", "XX"),
                 row("NSE", "D", "1", "OPTIDX", "NIFTY", "NIFTY", "N", "65",
                     "2026-08-20", "24000.0", "CE"),
                 row("NSE", "D", "2", "OPTIDX", "BANKNIFTY", "BANKNIFTY", "B", "15",
                     "2026-08-20", "52000.0", "CE")]
        path = tmp_path / "s.csv"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        reg = InstrumentRegistry.load(path, underlyings=["NIFTY"])
        assert reg.option("NIFTY", OptionType.CE, 24_000.0, dt.date(2026, 8, 20))
        with pytest.raises(InstrumentError):
            reg.option("BANKNIFTY", OptionType.CE, 52_000.0, dt.date(2026, 8, 20))
        # Index rows survive the filter — they are what signals run on.
        assert reg.index("NIFTY").security_id == "13"


class TestNullExpirySentinel:
    """Dhan writes 0001-01-01 rather than blank on index and equity rows. It is a
    valid date, so it has to be rejected explicitly or it sorts ahead of every
    real expiry."""

    def test_sentinel_becomes_none(self):
        from orbot.broker.instruments import _parse_expiry
        assert _parse_expiry("0001-01-01") is None

    def test_blank_and_na_become_none(self):
        from orbot.broker.instruments import _parse_expiry
        assert _parse_expiry("") is None
        assert _parse_expiry("NA") is None

    def test_real_expiry_survives(self):
        from orbot.broker.instruments import _parse_expiry
        assert _parse_expiry("2026-08-27") == dt.date(2026, 8, 27)

    def test_index_row_has_no_expiry(self, registry):
        assert registry.index("NIFTY").expiry is None
        assert registry.equity("RELIANCE").expiry is None
