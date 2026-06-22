from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Mapping

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


APP_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = Path(__file__).resolve().parents[2]
LOCAL_ENV_PATH = APP_DIR / ".env"
PROJECT_ENV_PATH = PROJECT_DIR / ".env"
CONFIG_FIELD_TO_ENV = {
    "binance_leverage": "BINANCE_LEVERAGE",
    "binance_entry_offset_usd": "BINANCE_ENTRY_OFFSET_USD",
    "open_min_edge": "OPEN_MIN_EDGE",
    "cancel_min_edge": "CANCEL_MIN_EDGE",
    "close_max_spread": "CLOSE_MAX_SPREAD",
    "close_profit_usd_per_oz": "CLOSE_PROFIT_USD_PER_OZ",
    "max_pair_age_minutes": "MAX_PAIR_AGE_MINUTES",
    "aged_close_profit_usd_per_oz": "AGED_CLOSE_PROFIT_USD_PER_OZ",
    "min_locked_edge": "MIN_LOCKED_EDGE",
    "entry_confirm_ms": "ENTRY_CONFIRM_MS",
    "min_order_live_ms": "MIN_ORDER_LIVE_MS",
    "requote_cooldown_ms": "REQUOTE_COOLDOWN_MS",
    "max_order_age_ms": "MAX_ORDER_AGE_MS",
    "max_quote_age_ms": "MAX_QUOTE_AGE_MS",
    "max_hedge_delay_ms": "MAX_HEDGE_DELAY_MS",
    "max_unhedged_loss_usd_per_oz": "MAX_UNHEDGED_LOSS_USD_PER_OZ",
    "daily_loss_limit_usdt": "DAILY_LOSS_LIMIT_USDT",
    "add_edge_growth_usd": "ADD_EDGE_GROWTH_USD",
    "max_add_count": "MAX_ADD_COUNT",
    "negative_swap_close_before_minutes": "NEGATIVE_SWAP_CLOSE_BEFORE_MINUTES",
    "target_oz": "TARGET_OZ",
    "mt4_lot_size_oz": "MT4_LOT_SIZE_OZ",
    "mt4_slippage_points": "MT4_SLIPPAGE_POINTS",
    "loop_interval_ms": "LOOP_INTERVAL_MS",
    "paper_auto_fill": "PAPER_AUTO_FILL",
    "paper_fill_delay_ms": "PAPER_FILL_DELAY_MS",
}
SAFE_CONFIG_ENV_KEYS = set(CONFIG_FIELD_TO_ENV.values())
MODE_ENV_KEYS = {"LIVE_TRADING", "PAPER_MODE"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ENV_PATH, LOCAL_ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    live_trading: bool = Field(default=False, alias="LIVE_TRADING")
    paper_mode: bool = Field(default=True, alias="PAPER_MODE")
    service_host: str = Field(default="127.0.0.1", alias="SERVICE_HOST")
    service_port: int = Field(default=8011, alias="SERVICE_PORT")

    binance_symbol: str = Field(default="XAUUSDT", alias="BINANCE_SYMBOL")
    mt4_symbol: str = Field(default="XAUUSD", alias="MT4_SYMBOL")
    binance_api_key: SecretStr | None = Field(default=None, alias="BINANCE_API_KEY")
    binance_api_secret: SecretStr | None = Field(default=None, alias="BINANCE_API_SECRET")
    binance_base_url: str = Field(default="https://fapi.binance.com", alias="BINANCE_BASE_URL")
    binance_ws_url: str = Field(default="wss://fstream.binance.com", alias="BINANCE_WS_URL")
    binance_maker_fee_rate: Decimal | None = Field(default=None, alias="BINANCE_MAKER_FEE_RATE")
    binance_taker_fee_rate: Decimal = Field(default=Decimal("0.0005"), alias="BINANCE_TAKER_FEE_RATE")
    binance_tick_size: Decimal = Field(default=Decimal("0.01"), alias="BINANCE_TICK_SIZE")
    binance_qty_step: Decimal = Field(default=Decimal("0.001"), alias="BINANCE_QTY_STEP")
    binance_min_qty: Decimal = Field(default=Decimal("0.001"), alias="BINANCE_MIN_QTY")
    binance_leverage: int = Field(default=20, alias="BINANCE_LEVERAGE")
    binance_entry_offset_usd: Decimal = Field(default=Decimal("1"), alias="BINANCE_ENTRY_OFFSET_USD")

    mt4_bridge_token: SecretStr | None = Field(default=None, alias="MT4_BRIDGE_TOKEN")
    open_min_edge: Decimal = Field(default=Decimal("1.50"), alias="OPEN_MIN_EDGE")
    cancel_min_edge: Decimal = Field(default=Decimal("1.20"), alias="CANCEL_MIN_EDGE")
    close_max_spread: Decimal = Field(default=Decimal("0.30"), alias="CLOSE_MAX_SPREAD")
    close_profit_usd_per_oz: Decimal = Field(default=Decimal("0.80"), alias="CLOSE_PROFIT_USD_PER_OZ")
    max_pair_age_minutes: int = Field(default=60, alias="MAX_PAIR_AGE_MINUTES")
    aged_close_profit_usd_per_oz: Decimal = Field(default=Decimal("0.10"), alias="AGED_CLOSE_PROFIT_USD_PER_OZ")
    min_locked_edge: Decimal = Field(default=Decimal("0.80"), alias="MIN_LOCKED_EDGE")
    entry_confirm_ms: int = Field(default=1500, alias="ENTRY_CONFIRM_MS")
    min_order_live_ms: int = Field(default=3000, alias="MIN_ORDER_LIVE_MS")
    requote_cooldown_ms: int = Field(default=2000, alias="REQUOTE_COOLDOWN_MS")
    max_order_age_ms: int = Field(default=300, alias="MAX_ORDER_AGE_MS")
    max_quote_age_ms: int = Field(default=1500, alias="MAX_QUOTE_AGE_MS")
    max_hedge_delay_ms: int = Field(default=5000, alias="MAX_HEDGE_DELAY_MS")
    max_unhedged_loss_usd_per_oz: Decimal = Field(default=Decimal("0.80"), alias="MAX_UNHEDGED_LOSS_USD_PER_OZ")
    daily_loss_limit_usdt: Decimal = Field(default=Decimal("50"), alias="DAILY_LOSS_LIMIT_USDT")
    add_edge_growth_usd: Decimal = Field(default=Decimal("1"), alias="ADD_EDGE_GROWTH_USD")
    max_add_count: int = Field(default=5, alias="MAX_ADD_COUNT")
    negative_swap_close_before_minutes: int = Field(default=30, alias="NEGATIVE_SWAP_CLOSE_BEFORE_MINUTES")
    target_oz: Decimal = Field(default=Decimal("1"), alias="TARGET_OZ")
    mt4_lot_size_oz: Decimal = Field(default=Decimal("100"), alias="MT4_LOT_SIZE_OZ")
    mt4_slippage_points: int = Field(default=30, alias="MT4_SLIPPAGE_POINTS")
    sqlite_path: Path = Field(default=Path("data/arb.sqlite3"), alias="SQLITE_PATH")
    loop_interval_ms: int = Field(default=50, alias="LOOP_INTERVAL_MS")
    paper_auto_fill: bool = Field(default=True, alias="PAPER_AUTO_FILL")
    paper_fill_delay_ms: int = Field(default=50, alias="PAPER_FILL_DELAY_MS")

    @field_validator(
        "open_min_edge",
        "close_max_spread",
        "close_profit_usd_per_oz",
        "aged_close_profit_usd_per_oz",
        "cancel_min_edge",
        "min_locked_edge",
        "max_unhedged_loss_usd_per_oz",
        "daily_loss_limit_usdt",
        "add_edge_growth_usd",
        "target_oz",
        "mt4_lot_size_oz",
        "binance_entry_offset_usd",
        "binance_tick_size",
        "binance_qty_step",
    )
    @classmethod
    def positive_decimal(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("numeric risk and sizing values must be positive")
        return value

    @field_validator("binance_leverage")
    @classmethod
    def valid_leverage(cls, value: int) -> int:
        if value < 1 or value > 125:
            raise ValueError("binance leverage must be between 1 and 125")
        return value

    @field_validator(
        "entry_confirm_ms",
        "min_order_live_ms",
        "requote_cooldown_ms",
        "max_order_age_ms",
        "max_quote_age_ms",
        "max_hedge_delay_ms",
        "loop_interval_ms",
        "paper_fill_delay_ms",
        "negative_swap_close_before_minutes",
        "max_pair_age_minutes",
    )
    @classmethod
    def non_negative_or_positive_timing(cls, value: int) -> int:
        if value < 0:
            raise ValueError("timing values must not be negative")
        return value

    @field_validator("max_add_count")
    @classmethod
    def non_negative_add_count(cls, value: int) -> int:
        if value < 0:
            raise ValueError("max add count must not be negative")
        return value

    @property
    def is_dry_run(self) -> bool:
        return self.paper_mode or not self.live_trading


def load_settings() -> Settings:
    return Settings()


def existing_env_paths() -> list[Path]:
    return [path for path in (LOCAL_ENV_PATH, PROJECT_ENV_PATH) if path.exists()]


def env_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def update_local_config_file(values: Mapping[str, object], path: Path = LOCAL_ENV_PATH) -> None:
    updates = {
        CONFIG_FIELD_TO_ENV[field]: env_value(value)
        for field, value in values.items()
        if field in CONFIG_FIELD_TO_ENV
    }
    if not updates:
        return

    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    written: set[str] = set()
    next_lines: list[str] = []

    for line in existing_lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in updates and key in SAFE_CONFIG_ENV_KEYS:
            next_lines.append(f"{key}={updates[key]}")
            written.add(key)
        else:
            next_lines.append(line)

    for field, env_key in CONFIG_FIELD_TO_ENV.items():
        if env_key in updates and env_key not in written:
            next_lines.append(f"{env_key}={updates[env_key]}")

    path.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")


def update_mode_file(live_trading: bool, paper_mode: bool, path: Path = LOCAL_ENV_PATH) -> None:
    updates = {
        "LIVE_TRADING": env_value(live_trading),
        "PAPER_MODE": env_value(paper_mode),
    }
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    written: set[str] = set()
    next_lines: list[str] = []
    for line in existing_lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in MODE_ENV_KEYS:
            next_lines.append(f"{key}={updates[key]}")
            written.add(key)
        else:
            next_lines.append(line)
    for key in ("LIVE_TRADING", "PAPER_MODE"):
        if key not in written:
            next_lines.append(f"{key}={updates[key]}")
    path.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")
