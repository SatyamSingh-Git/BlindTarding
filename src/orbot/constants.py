"""Dhan API enums and wire codes.

Values are taken verbatim from the DhanHQ v2 annexure. The integer codes matter
because the WebSocket feed sends the exchange segment as a single byte, while the
REST API wants the string name.
"""

from __future__ import annotations

from enum import Enum, IntEnum

IST = "Asia/Kolkata"


class ExchangeSegment(str, Enum):
    """REST uses the name, the binary feed uses `.code`."""

    IDX_I = "IDX_I"
    NSE_EQ = "NSE_EQ"
    NSE_FNO = "NSE_FNO"
    NSE_CURRENCY = "NSE_CURRENCY"
    BSE_EQ = "BSE_EQ"
    MCX_COMM = "MCX_COMM"
    BSE_CURRENCY = "BSE_CURRENCY"
    BSE_FNO = "BSE_FNO"

    @property
    def code(self) -> int:
        return _SEGMENT_CODES[self]

    @classmethod
    def from_code(cls, code: int) -> ExchangeSegment:
        return _CODE_TO_SEGMENT[code]


_SEGMENT_CODES: dict[ExchangeSegment, int] = {
    ExchangeSegment.IDX_I: 0,
    ExchangeSegment.NSE_EQ: 1,
    ExchangeSegment.NSE_FNO: 2,
    ExchangeSegment.NSE_CURRENCY: 3,
    ExchangeSegment.BSE_EQ: 4,
    ExchangeSegment.MCX_COMM: 5,
    ExchangeSegment.BSE_CURRENCY: 7,
    ExchangeSegment.BSE_FNO: 8,
}
_CODE_TO_SEGMENT = {v: k for k, v in _SEGMENT_CODES.items()}

# (EXCH_ID, SEGMENT) as they appear in the scrip master CSV -> API segment.
# Verified against api-scrip-master-detailed.csv.
CSV_SEGMENT_MAP: dict[tuple[str, str], ExchangeSegment] = {
    ("NSE", "I"): ExchangeSegment.IDX_I,
    ("BSE", "I"): ExchangeSegment.IDX_I,
    ("MCX", "I"): ExchangeSegment.IDX_I,
    ("NSE", "E"): ExchangeSegment.NSE_EQ,
    ("NSE", "D"): ExchangeSegment.NSE_FNO,
    ("NSE", "C"): ExchangeSegment.NSE_CURRENCY,
    ("BSE", "E"): ExchangeSegment.BSE_EQ,
    ("BSE", "D"): ExchangeSegment.BSE_FNO,
    ("BSE", "C"): ExchangeSegment.BSE_CURRENCY,
    ("MCX", "M"): ExchangeSegment.MCX_COMM,
}


class InstrumentType(str, Enum):
    INDEX = "INDEX"
    EQUITY = "EQUITY"
    FUTIDX = "FUTIDX"
    FUTSTK = "FUTSTK"
    FUTCOM = "FUTCOM"
    FUTCUR = "FUTCUR"
    OPTIDX = "OPTIDX"
    OPTSTK = "OPTSTK"
    OPTFUT = "OPTFUT"
    OPTCUR = "OPTCUR"


OPTION_TYPES = {InstrumentType.OPTIDX, InstrumentType.OPTSTK,
                InstrumentType.OPTFUT, InstrumentType.OPTCUR}
FUTURE_TYPES = {InstrumentType.FUTIDX, InstrumentType.FUTSTK,
                InstrumentType.FUTCOM, InstrumentType.FUTCUR}


class TransactionType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ProductType(str, Enum):
    CNC = "CNC"
    INTRADAY = "INTRADAY"
    MARGIN = "MARGIN"
    MTF = "MTF"
    CO = "CO"
    BO = "BO"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    STOP_LOSS = "STOP_LOSS"
    STOP_LOSS_MARKET = "STOP_LOSS_MARKET"


class Validity(str, Enum):
    DAY = "DAY"
    IOC = "IOC"


class OrderStatus(str, Enum):
    TRANSIT = "TRANSIT"
    PENDING = "PENDING"
    CLOSED = "CLOSED"
    TRIGGERED = "TRIGGERED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    PART_TRADED = "PART_TRADED"
    TRADED = "TRADED"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATUSES

    @property
    def is_fill(self) -> bool:
        return self in (OrderStatus.TRADED, OrderStatus.PART_TRADED)


_TERMINAL_STATUSES = frozenset({
    OrderStatus.TRADED, OrderStatus.REJECTED,
    OrderStatus.CANCELLED, OrderStatus.CLOSED,
})


class OptionType(str, Enum):
    CE = "CE"
    PE = "PE"


class FeedCode(IntEnum):
    """First byte of every binary feed packet."""

    INDEX = 1
    TICKER = 2
    QUOTE = 4
    OI = 5
    PREV_CLOSE = 6
    FULL = 8
    DISCONNECT = 50


class FeedRequestCode(IntEnum):
    SUBSCRIBE_TICKER = 15
    UNSUBSCRIBE_TICKER = 16
    SUBSCRIBE_QUOTE = 17
    UNSUBSCRIBE_QUOTE = 18
    SUBSCRIBE_FULL = 21
    UNSUBSCRIBE_FULL = 22
    DISCONNECT = 12


# --- API endpoints ------------------------------------------------------
API_BASE = "https://api.dhan.co/v2"
FEED_URL = "wss://api-feed.dhan.co"
SCRIP_MASTER_DETAILED = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
SCRIP_MASTER_COMPACT = "https://images.dhan.co/api-data/api-scrip-master.csv"

# --- Rate limits (Dhan published) --------------------------------------
# We deliberately run well under these; see execution.guards.
MAX_ORDERS_PER_SECOND = 25
MAX_ORDERS_PER_DAY = 7000
MAX_INSTRUMENTS_PER_SUBSCRIBE_MSG = 100
MAX_INSTRUMENTS_PER_CONNECTION = 5000
MAX_FEED_CONNECTIONS = 5

# Intraday historical intervals Dhan supports, in minutes.
INTRADAY_INTERVALS = (1, 5, 15, 25, 60)
HISTORICAL_MAX_DAYS_PER_CALL = 90
