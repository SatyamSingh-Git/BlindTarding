"""Angel One scrip master.

Angel publishes a single JSON file of every tradeable instrument. It is parsed
into the same `Instrument` model the Dhan adapter produces, so the existing
`InstrumentRegistry` — with all its strike selection, expiry rules and lookup
logic — is reused unchanged. Only the parsing differs.

**Strike and tick size arrive in paise.** A NIFTY 30000 PE carries
`strike: "3000000.000000"` and a tick size of `"5.000000"` meaning 5 paise.
Dividing by 100 is not cosmetic: an unscaled strike would be 100x wrong, and
`select_option` would then pick a real contract at an absurd strike — an order
that looks perfectly normal right up until it fills.

Angel's instrument ids ("token") are **different numbers from Dhan's
securityId for the same contract**. Nothing outside the broker layer builds one;
they only ever come from a registry.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Iterable, Iterator, Sequence

import httpx
import structlog

from ...constants import ExchangeSegment, InstrumentType, OptionType
from ...models import Instrument
from ..instruments import InstrumentRegistry

log = structlog.get_logger(__name__)

SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)

# Angel quotes strike and tick size in paise.
PAISE = 100.0

# Angel's WebSocket exchange codes, from the official SDK.
EXCHANGE_TYPE_CODES: dict[ExchangeSegment, int] = {
    ExchangeSegment.NSE_EQ: 1,
    ExchangeSegment.IDX_I: 1,        # indices stream on the NSE cash feed
    ExchangeSegment.NSE_FNO: 2,
    ExchangeSegment.BSE_EQ: 3,
    ExchangeSegment.BSE_FNO: 4,
    ExchangeSegment.MCX_COMM: 5,
    ExchangeSegment.NSE_CURRENCY: 13,
    ExchangeSegment.BSE_CURRENCY: 13,
}

# Angel's REST `exchange` parameter.
EXCHANGE_NAMES: dict[ExchangeSegment, str] = {
    ExchangeSegment.IDX_I: "NSE",
    ExchangeSegment.NSE_EQ: "NSE",
    ExchangeSegment.NSE_FNO: "NFO",
    ExchangeSegment.BSE_EQ: "BSE",
    ExchangeSegment.BSE_FNO: "BFO",
    ExchangeSegment.MCX_COMM: "MCX",
    ExchangeSegment.NSE_CURRENCY: "CDS",
    ExchangeSegment.BSE_CURRENCY: "CDS",
}

_SEGMENT_FOR_EXCHANGE: dict[str, ExchangeSegment] = {
    "NFO": ExchangeSegment.NSE_FNO,
    "BFO": ExchangeSegment.BSE_FNO,
    "MCX": ExchangeSegment.MCX_COMM,
    "CDS": ExchangeSegment.NSE_CURRENCY,
    "NCDEX": ExchangeSegment.MCX_COMM,
}

_INSTRUMENT_TYPES = {t.value for t in InstrumentType}
_INDEX_MARKERS = {"AMXIDX", "INDEX", "IDX"}


def download_scrip_master(dest: Path, *, timeout: float = 180.0) -> Path:
    """Fetch Angel's instrument JSON, written atomically so a partial download
    can never be mistaken for a good cache."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", SCRIP_MASTER_URL, timeout=timeout,
                      follow_redirects=True) as resp:
        resp.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1 << 20):
                fh.write(chunk)
    tmp.replace(dest)
    return dest


def _to_float(raw: object, default: float = 0.0) -> float:
    try:
        return float(str(raw))
    except (TypeError, ValueError):
        return default


def parse_expiry(raw: object) -> dt.date | None:
    """Angel writes expiries as `29SEP2026`, and blank for cash and indices."""
    text = str(raw or "").strip().upper()
    if not text or text in {"NA", "0"}:
        return None
    for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _classify(row: dict) -> tuple[ExchangeSegment, InstrumentType] | None:
    """Map Angel's `exch_seg` + `instrumenttype` onto our shared vocabulary."""
    exchange = str(row.get("exch_seg", "")).strip().upper()
    raw_type = str(row.get("instrumenttype", "")).strip().upper()
    symbol = str(row.get("symbol", "")).strip().upper()

    if exchange in _SEGMENT_FOR_EXCHANGE:
        if raw_type not in _INSTRUMENT_TYPES:
            return None                    # e.g. spreads, which we never trade
        return _SEGMENT_FOR_EXCHANGE[exchange], InstrumentType(raw_type)

    if exchange in ("NSE", "BSE"):
        # Verified against the file: every index carries instrumenttype AMXIDX,
        # on both exchanges. Everything else with a blank type is cash.
        if raw_type in _INDEX_MARKERS:
            return ExchangeSegment.IDX_I, InstrumentType.INDEX
        if raw_type:
            return None                       # a type we do not trade
        if exchange == "BSE":
            # BSE cash carries no -EQ suffix, so blank type is the only marker.
            return ExchangeSegment.BSE_EQ, InstrumentType.EQUITY
        if symbol.endswith("-EQ"):
            return ExchangeSegment.NSE_EQ, InstrumentType.EQUITY
        # An NSE row with no type and no -EQ suffix is neither cleanly: dropping
        # it beats guessing, since guessing wrong here means a plan configured
        # against something that cannot be traded.
        return None
    return None


def parse_row(row: dict) -> Instrument | None:
    classified = _classify(row)
    if classified is None:
        return None
    segment, itype = classified

    token = str(row.get("token", "")).strip()
    if not token:
        return None

    symbol = str(row.get("symbol", "")).strip()
    name = str(row.get("name", "")).strip()

    strike_paise = _to_float(row.get("strike"), -1.0)
    strike = strike_paise / PAISE if strike_paise > 0 else None
    option_type = None
    if itype.value.startswith("OPT"):
        upper = symbol.upper()
        if upper.endswith("CE"):
            option_type = OptionType.CE
        elif upper.endswith("PE"):
            option_type = OptionType.PE
    if option_type is None:
        strike = None

    tick = _to_float(row.get("tick_size"), 5.0) / PAISE

    return Instrument(
        security_id=token,
        segment=segment,
        instrument_type=itype,
        symbol=(name or symbol).upper(),
        display_name=symbol or name,
        lot_size=max(1, int(_to_float(row.get("lotsize"), 1.0))),
        tick_size=tick if tick > 0 else 0.05,
        expiry=parse_expiry(row.get("expiry")),
        strike=strike,
        option_type=option_type,
        underlying_symbol=(name or None),
        series="EQ" if symbol.upper().endswith("-EQ") else None,
        freeze_qty=None,          # Angel does not publish freeze limits here
    )


def iter_instruments(path: Path) -> Iterator[Instrument]:
    raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(raw, list):
        raise ValueError(f"{path} is not an Angel scrip master (expected a JSON list)")
    for row in raw:
        if not isinstance(row, dict):
            continue
        instrument = parse_row(row)
        if instrument is not None:
            yield instrument


def load_registry(
    path: Path,
    *,
    segments: Sequence[ExchangeSegment] | None = None,
    underlyings: Sequence[str] | None = None,
) -> InstrumentRegistry:
    """Parse Angel's JSON into the shared registry.

    Filtering happens here for the same reason as the Dhan adapter: the full
    file is hundreds of thousands of rows and most of them are contracts on
    underlyings this bot will never touch.
    """
    from ...constants import FUTURE_TYPES, OPTION_TYPES

    seg_filter = set(segments) if segments else None
    und_filter = {u.upper() for u in underlyings} if underlyings else None

    kept: list[Instrument] = []
    for instrument in iter_instruments(path):
        if seg_filter and instrument.segment not in seg_filter:
            continue
        if und_filter and instrument.instrument_type in (OPTION_TYPES | FUTURE_TYPES):
            und = (instrument.underlying_symbol or instrument.symbol or "").upper()
            if und not in und_filter:
                continue
        kept.append(instrument)
    log.info("angel.instruments_loaded", count=len(kept))
    return InstrumentRegistry(kept)


def exchange_name(segment: ExchangeSegment) -> str:
    try:
        return EXCHANGE_NAMES[segment]
    except KeyError:
        raise ValueError(f"Angel has no exchange for segment {segment.value}") from None


def exchange_type_code(segment: ExchangeSegment) -> int:
    try:
        return EXCHANGE_TYPE_CODES[segment]
    except KeyError:
        raise ValueError(f"Angel has no feed code for segment {segment.value}") from None


def segment_for_code(code: int, *, default: ExchangeSegment = ExchangeSegment.NSE_EQ
                     ) -> ExchangeSegment:
    """Reverse of `exchange_type_code`.

    Ambiguous by construction: indices and NSE cash share code 1, so a tick's
    true segment is recovered by matching the token against what we subscribed,
    not from this. Used only for logging.
    """
    for segment, value in EXCHANGE_TYPE_CODES.items():
        if value == code:
            return segment
    return default


__all__ = [
    "download_scrip_master", "load_registry", "iter_instruments", "parse_row",
    "parse_expiry", "exchange_name", "exchange_type_code", "segment_for_code",
    "SCRIP_MASTER_URL", "PAISE", "EXCHANGE_TYPE_CODES", "EXCHANGE_NAMES",
]
