"""Scrip-master registry and instrument resolution.

Dhan identifies everything by (exchangeSegment, securityId). Humans think in
"NIFTY ATM CE, nearest weekly". This module is the translation layer, and it is
what lets one engine trade equities, futures and options without special-casing.

The detailed CSV is ~34 MB / ~200k rows, so `load()` filters at parse time —
only keep segments and underlyings you actually trade.
"""

from __future__ import annotations

import csv
import datetime as dt
import math
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Sequence

import httpx

from ..constants import (
    CSV_SEGMENT_MAP,
    FUTURE_TYPES,
    OPTION_TYPES,
    SCRIP_MASTER_DETAILED,
    ExchangeSegment,
    InstrumentType,
    OptionType,
)
from ..models import Instrument

# Column indices are avoided on purpose — Dhan has added columns before.
_C_EXCH = "EXCH_ID"
_C_SEG = "SEGMENT"
_C_SECID = "SECURITY_ID"
_C_INSTR = "INSTRUMENT"
_C_UND_SYM = "UNDERLYING_SYMBOL"
_C_SYMBOL = "SYMBOL_NAME"
_C_DISPLAY = "DISPLAY_NAME"
_C_SERIES = "SERIES"
_C_LOT = "LOT_SIZE"
_C_EXPIRY = "SM_EXPIRY_DATE"
_C_STRIKE = "STRIKE_PRICE"
_C_OPTTYPE = "OPTION_TYPE"
_C_TICK = "TICK_SIZE"
_C_FREEZE = "SM_FREEZE_QTY"


class InstrumentError(LookupError):
    """Raised when a requested instrument cannot be resolved. Never guess —
    a wrong securityId is a wrong trade."""


def download_scrip_master(dest: Path, *, timeout: float = 180.0) -> Path:
    """Fetch the detailed scrip master. Writes atomically so a partial download
    can never be mistaken for a good cache."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", SCRIP_MASTER_DETAILED, timeout=timeout,
                      follow_redirects=True) as resp:
        resp.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1 << 20):
                fh.write(chunk)
    tmp.replace(dest)
    return dest


def is_fresh(path: Path, *, today: dt.date | None = None) -> bool:
    """Contracts change daily (new expiries, revised lot sizes). Anything not
    written today is stale."""
    if not path.exists():
        return False
    today = today or dt.date.today()
    mtime = dt.date.fromtimestamp(path.stat().st_mtime)
    return mtime >= today


def _to_float(raw: str, default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _to_int(raw: str, default: int = 0) -> int:
    return int(_to_float(raw, default))


# Dhan writes 0001-01-01 as the null expiry on index and equity rows. It parses
# as a perfectly valid date, so it has to be rejected explicitly — otherwise it
# sorts ahead of every real expiry and "nearest" silently picks nothing usable.
_MIN_PLAUSIBLE_EXPIRY = dt.date(1980, 1, 1)


def _parse_expiry(raw: str) -> dt.date | None:
    raw = (raw or "").strip()
    if not raw or raw.upper() in {"NA", "N/A", "0", "-1"}:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            parsed = dt.datetime.strptime(raw[:19], fmt).date()
        except ValueError:
            continue
        return parsed if parsed >= _MIN_PLAUSIBLE_EXPIRY else None
    return None


def _parse_row(row: dict[str, str]) -> Instrument | None:
    segment = CSV_SEGMENT_MAP.get(
        ((row.get(_C_EXCH) or "").strip(), (row.get(_C_SEG) or "").strip())
    )
    if segment is None:
        return None
    try:
        itype = InstrumentType((row.get(_C_INSTR) or "").strip())
    except ValueError:
        return None

    security_id = (row.get(_C_SECID) or "").strip()
    if not security_id:
        return None

    opt_raw = (row.get(_C_OPTTYPE) or "").strip().upper()
    option_type = OptionType(opt_raw) if opt_raw in ("CE", "PE") else None
    strike = _to_float(row.get(_C_STRIKE, ""), -1.0)

    return Instrument(
        security_id=security_id,
        segment=segment,
        instrument_type=itype,
        symbol=(row.get(_C_SYMBOL) or "").strip(),
        display_name=(row.get(_C_DISPLAY) or "").strip(),
        lot_size=max(1, _to_int(row.get(_C_LOT, ""), 1)),
        tick_size=_to_float(row.get(_C_TICK, ""), 0.05) or 0.05,
        expiry=_parse_expiry(row.get(_C_EXPIRY, "")),
        strike=strike if (option_type and strike > 0) else None,
        option_type=option_type,
        underlying_symbol=(row.get(_C_UND_SYM) or "").strip() or None,
        series=(row.get(_C_SERIES) or "").strip() or None,
        freeze_qty=_to_int(row.get(_C_FREEZE, ""), 0) or None,
    )


class InstrumentRegistry:
    """In-memory index over the scrip master."""

    def __init__(self, instruments: Iterable[Instrument]) -> None:
        self._by_key: dict[tuple[str, str], Instrument] = {}
        self._by_symbol: dict[tuple[ExchangeSegment, InstrumentType, str], list[Instrument]] = (
            defaultdict(list)
        )
        # (underlying, instrument_type) -> expiry -> option_type -> {strike: Instrument}
        self._chains: dict[
            tuple[str, InstrumentType],
            dict[dt.date, dict[OptionType, dict[float, Instrument]]],
        ] = defaultdict(lambda: defaultdict(lambda: defaultdict(dict)))
        self._futures: dict[tuple[str, InstrumentType], dict[dt.date, Instrument]] = (
            defaultdict(dict)
        )

        for inst in instruments:
            self._by_key[inst.key] = inst
            if inst.symbol:
                self._by_symbol[(inst.segment, inst.instrument_type, inst.symbol.upper())].append(
                    inst
                )
            und = (inst.underlying_symbol or inst.symbol or "").upper()
            if inst.instrument_type in OPTION_TYPES and inst.expiry and inst.strike:
                self._chains[(und, inst.instrument_type)][inst.expiry][
                    inst.option_type
                ][inst.strike] = inst
            elif inst.instrument_type in FUTURE_TYPES and inst.expiry:
                self._futures[(und, inst.instrument_type)][inst.expiry] = inst

        self._strike_cache: dict[tuple[str, InstrumentType, dt.date, OptionType],
                                 list[float]] = {}

    # --- construction ---------------------------------------------------
    @classmethod
    def load(
        cls,
        path: Path,
        *,
        segments: Sequence[ExchangeSegment] | None = None,
        underlyings: Sequence[str] | None = None,
    ) -> InstrumentRegistry:
        """Parse the CSV, keeping only rows worth holding in memory.

        `underlyings` filters derivatives only — index and equity rows are always
        kept, since they are cheap and are what signals run on.
        """
        seg_filter = set(segments) if segments else None
        und_filter = {u.upper() for u in underlyings} if underlyings else None

        kept: list[Instrument] = []
        with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
            for row in csv.DictReader(fh):
                inst = _parse_row(row)
                if inst is None:
                    continue
                if seg_filter and inst.segment not in seg_filter:
                    continue
                if und_filter and inst.instrument_type in (OPTION_TYPES | FUTURE_TYPES):
                    und = (inst.underlying_symbol or inst.symbol or "").upper()
                    if und not in und_filter:
                        continue
                kept.append(inst)
        return cls(kept)

    def __len__(self) -> int:
        return len(self._by_key)

    # --- direct lookup --------------------------------------------------
    def get(self, segment: ExchangeSegment, security_id: str) -> Instrument:
        try:
            return self._by_key[(segment.value, str(security_id))]
        except KeyError:
            raise InstrumentError(
                f"no instrument {segment.value}/{security_id} in scrip master"
            ) from None

    def find(
        self,
        symbol: str,
        segment: ExchangeSegment,
        instrument_type: InstrumentType,
        *,
        series: str | None = None,
    ) -> Instrument:
        matches = self._by_symbol.get((segment, instrument_type, symbol.upper()), [])
        if series:
            matches = [m for m in matches if (m.series or "").upper() == series.upper()]
        if not matches:
            raise InstrumentError(
                f"no {instrument_type.value} named {symbol!r} in {segment.value}"
            )
        if len(matches) > 1 and instrument_type is InstrumentType.EQUITY:
            # EQ is the normal cash series; BE/BZ are surveillance segments.
            eq = [m for m in matches if (m.series or "").upper() == "EQ"]
            if eq:
                return eq[0]
        return matches[0]

    def index(self, symbol: str) -> Instrument:
        """NIFTY, BANKNIFTY, SENSEX, ..."""
        return self.find(symbol, ExchangeSegment.IDX_I, InstrumentType.INDEX)

    def equity(self, symbol: str, *, exchange: ExchangeSegment = ExchangeSegment.NSE_EQ
               ) -> Instrument:
        return self.find(symbol, exchange, InstrumentType.EQUITY, series="EQ")

    # --- expiries -------------------------------------------------------
    def expiries(
        self, underlying: str, instrument_type: InstrumentType, *,
        on_or_after: dt.date | None = None,
    ) -> list[dt.date]:
        und = underlying.upper()
        if instrument_type in OPTION_TYPES:
            available = self._chains.get((und, instrument_type), {}).keys()
        else:
            available = self._futures.get((und, instrument_type), {}).keys()
        floor = on_or_after or dt.date.today()
        return sorted(e for e in available if e >= floor)

    def select_expiry(
        self, underlying: str, instrument_type: InstrumentType, rule: str,
        *, today: dt.date | None = None,
    ) -> dt.date:
        """`rule` is one of: nearest, next, nearest_monthly, next_monthly,
        or an explicit YYYY-MM-DD."""
        today = today or dt.date.today()
        explicit = _parse_expiry(rule)
        if explicit is not None:
            return explicit

        avail = self.expiries(underlying, instrument_type, on_or_after=today)
        if not avail:
            raise InstrumentError(
                f"no live expiries for {underlying} {instrument_type.value}"
            )

        rule = rule.strip().lower()
        if rule in ("nearest", "nearest_weekly", "current"):
            return avail[0]
        if rule in ("next", "next_weekly"):
            return avail[1] if len(avail) > 1 else avail[0]
        if rule in ("nearest_monthly", "monthly", "next_monthly"):
            monthly = _monthly_expiries(avail)
            if not monthly:
                return avail[-1]
            return monthly[1] if rule == "next_monthly" and len(monthly) > 1 else monthly[0]
        raise InstrumentError(f"unknown expiry rule {rule!r}")

    # --- options --------------------------------------------------------
    def strikes(
        self, underlying: str, instrument_type: InstrumentType,
        expiry: dt.date, option_type: OptionType,
    ) -> list[float]:
        key = (underlying.upper(), instrument_type, expiry, option_type)
        cached = self._strike_cache.get(key)
        if cached is None:
            chain = self._chains.get((underlying.upper(), instrument_type), {})
            cached = sorted(chain.get(expiry, {}).get(option_type, {}))
            self._strike_cache[key] = cached
        return cached

    def option(
        self,
        underlying: str,
        option_type: OptionType,
        strike: float,
        expiry: dt.date,
        *,
        instrument_type: InstrumentType = InstrumentType.OPTIDX,
    ) -> Instrument:
        chain = self._chains.get((underlying.upper(), instrument_type), {})
        inst = chain.get(expiry, {}).get(option_type, {}).get(strike)
        if inst is None:
            raise InstrumentError(
                f"no {underlying} {expiry} {strike} {option_type.value} contract"
            )
        return inst

    def select_option(
        self,
        underlying: str,
        option_type: OptionType,
        spot: float,
        expiry: dt.date,
        *,
        instrument_type: InstrumentType = InstrumentType.OPTIDX,
        offset: int = 0,
    ) -> Instrument:
        """Pick a strike relative to ATM.

        `offset` counts strike steps in the direction of increasing moneyness:
        for a CE, +1 is one strike ITM and -1 is one strike OTM; for a PE the
        sign is mirrored so that +1 is always ITM. This keeps config readable —
        `strike: ITM1` means the same thing for calls and puts.
        """
        ladder = self.strikes(underlying, instrument_type, expiry, option_type)
        if not ladder:
            raise InstrumentError(
                f"empty {option_type.value} chain for {underlying} {expiry}"
            )
        # bisect on a NaN returns an arbitrary index rather than raising, so a
        # poisoned spot would quietly select a real but wrong strike — the worst
        # possible outcome, since the resulting order looks entirely normal.
        if not math.isfinite(spot):
            raise InstrumentError(
                f"cannot select a {underlying} strike from a non-finite spot ({spot})"
            )

        idx = bisect_left(ladder, spot)
        if idx >= len(ladder):
            idx = len(ladder) - 1
        elif idx > 0 and abs(ladder[idx - 1] - spot) <= abs(ladder[idx] - spot):
            idx -= 1

        step = -offset if option_type is OptionType.CE else offset
        target = min(max(idx + step, 0), len(ladder) - 1)
        return self.option(underlying, option_type, ladder[target], expiry,
                           instrument_type=instrument_type)

    # --- futures --------------------------------------------------------
    def future(
        self, underlying: str, expiry: dt.date, *,
        instrument_type: InstrumentType = InstrumentType.FUTIDX,
    ) -> Instrument:
        inst = self._futures.get((underlying.upper(), instrument_type), {}).get(expiry)
        if inst is None:
            raise InstrumentError(f"no {underlying} {expiry} future")
        return inst


def _monthly_expiries(expiries: Sequence[dt.date]) -> list[dt.date]:
    """The last expiry in each calendar month — the monthly contract."""
    last: dict[tuple[int, int], dt.date] = {}
    for e in expiries:
        key = (e.year, e.month)
        if key not in last or e > last[key]:
            last[key] = e
    return sorted(last.values())
