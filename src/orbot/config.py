"""Settings and trade-plan configuration.

Secrets come from `.env`; what to trade comes from `config/instruments.yaml`.
The split matters: plans get edited often and are safe to read, credentials are
neither.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .constants import ExchangeSegment, InstrumentType, OrderType, ProductType
from .engine.timeframe import Timeframe

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
HALT_FILE = ROOT / "HALT"


class Settings(BaseSettings):
    """Everything from `.env`."""

    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    dhan_client_id: str = ""
    dhan_access_token: str = ""

    # Dhan requires a whitelisted static IP for order placement. Empty means we
    # call direct, which only works if this machine's own IP is whitelisted.
    dhan_order_proxy: str = ""
    proxy_data_calls: bool = False

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    trade_mode: Literal["paper", "live"] = "paper"
    log_level: str = "INFO"

    # Local safety caps, deliberately far below Dhan's 25/sec and 7000/day.
    max_orders_per_second: int = 5
    max_orders_per_day: int = 500

    @property
    def is_live(self) -> bool:
        return self.trade_mode == "live"

    @property
    def scrip_master_path(self) -> Path:
        return DATA_DIR / "scrip_master.csv"

    @property
    def journal_path(self) -> Path:
        return DATA_DIR / "journal.db"

    def missing_credentials(self) -> list[str]:
        missing = []
        if not self.dhan_client_id:
            missing.append("DHAN_CLIENT_ID")
        if not self.dhan_access_token:
            missing.append("DHAN_ACCESS_TOKEN")
        return missing


class SignalConfig(BaseModel):
    """What the indicator is computed on."""

    exchange_segment: ExchangeSegment
    security_id: str
    instrument_type: InstrumentType = InstrumentType.INDEX
    # Anything from "5m" to "2D". Validated at load so a typo fails at startup
    # rather than halfway through a trading session.
    timeframe: str = "5m"

    @field_validator("security_id", mode="before")
    @classmethod
    def _stringify(cls, v: Any) -> str:
        return str(v)

    @field_validator("timeframe", mode="before")
    @classmethod
    def _normalise_timeframe(cls, v: Any) -> str:
        return Timeframe.parse(v).label

    @property
    def tf(self) -> Timeframe:
        return Timeframe.parse(self.timeframe)


class StrategyConfig(BaseModel):
    preset: str = "svmkr_ut_hma_orb"
    # Anything omitted falls back to the values read off the live chart legend
    # ("3 1 31 5 1010-1015"), NOT the Pine input() defaults — the source
    # defaults ut_key to 2 where the chart runs 3.
    params: dict[str, Any] = Field(default_factory=dict)
    signal_on_bar_close: bool = True


class LegConfig(BaseModel):
    """How a direction maps to a tradeable contract."""

    option_type: Literal["CE", "PE"] | None = None
    strike: str = "ATM"          # ATM | ITM1 | OTM2 | an absolute strike
    expiry: str = "nearest"      # nearest | next | nearest_monthly | YYYY-MM-DD


class ExecutionConfig(BaseModel):
    mode: Literal["same", "option", "future"] = "same"
    underlying: str = ""
    on_buy: LegConfig | None = None
    # SELL means "exit the long and go flat" — decided in PLAN.md open question 2.
    on_sell: Literal["exit"] | LegConfig = "exit"
    product: ProductType = ProductType.INTRADAY
    order_type: OrderType = OrderType.MARKET
    qty_lots: int = 1
    quantity: int | None = None   # absolute qty; overrides qty_lots for cash


class PlanRisk(BaseModel):
    sl_pct: float | None = None
    target_pct: float | None = None
    max_trades_per_day: int = 4
    square_off_time: dt.time = dt.time(15, 10)


class TradePlan(BaseModel):
    name: str
    enabled: bool = True
    signal: SignalConfig
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    risk: PlanRisk = Field(default_factory=PlanRisk)


class GlobalRisk(BaseModel):
    """Account-wide limits. Any breach halts everything, not just one plan."""

    max_daily_loss: float = 5000.0
    max_open_positions: int = 3
    max_notional_per_order: float = 200_000.0
    max_qty_per_order: int = 10_000
    feed_staleness_seconds: int = 60
    signal_staleness_seconds: int = 120
    reconcile_interval_seconds: int = 30
    market_open: dt.time = dt.time(9, 15)
    market_close: dt.time = dt.time(15, 30)
    flatten_on_halt: bool = True


def load_plans(path: Path | None = None) -> list[TradePlan]:
    path = path or (CONFIG_DIR / "instruments.yaml")
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [TradePlan.model_validate(p) for p in raw.get("plans", [])]


def load_risk(path: Path | None = None) -> GlobalRisk:
    path = path or (CONFIG_DIR / "risk.yaml")
    if not path.exists():
        return GlobalRisk()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return GlobalRisk.model_validate(raw)


def get_settings() -> Settings:
    return Settings()


__all__ = [
    "Settings", "TradePlan", "GlobalRisk", "SignalConfig", "StrategyConfig",
    "ExecutionConfig", "LegConfig", "PlanRisk",
    "load_plans", "load_risk", "get_settings",
    "ROOT", "CONFIG_DIR", "DATA_DIR", "HALT_FILE",
]
