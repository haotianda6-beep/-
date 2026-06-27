from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ExchangeName(str, Enum):
    OKX = "OKX"
    GATE = "GATE"
    BITGET = "BITGET"
    BYBIT = "BYBIT"
    BINANCE = "BINANCE"


class DataSource(str, Enum):
    MOCK = "mock"
    LIVE = "live"


class ReconcileStatus(str, Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    MISMATCH = "mismatch"

class BaseSchema(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

class ExchangeBalance(BaseSchema):
    exchange: ExchangeName
    equity_usdt: Decimal
    available_usdt: Decimal
    margin_used_usdt: Decimal
    updated_at: datetime

class PositionSnapshot(BaseSchema):
    exchange: ExchangeName
    symbol: str
    side: Literal["long", "short"]
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    leverage: Decimal
    unrealized_pnl: Decimal
    liquidation_price: Decimal | None = None

class CashCarryPositionRow(BaseSchema):
    exchange: ExchangeName
    symbol: str
    status: Literal["matched", "mismatch", "spot_only", "perp_only"]
    spot_quantity: Decimal
    spot_entry_price: Decimal
    spot_price: Decimal
    spot_unrealized_pnl: Decimal
    perp_side: Literal["short", "long", "none"]
    perp_contracts: Decimal
    perp_base_quantity: Decimal
    contract_size: Decimal
    perp_entry_price: Decimal
    perp_mark_price: Decimal
    leverage: Decimal
    perp_unrealized_pnl: Decimal
    estimated_funding_rate_pct: Decimal
    estimated_funding_income: Decimal
    estimated_open_fee: Decimal
    estimated_close_fee: Decimal
    current_net_profit: Decimal
    quantity_gap: Decimal
    basis_pct: Decimal
    add_count: int = 0
    add_notional_usdt: Decimal = Decimal("0")
    next_add_trigger_basis_pct: Decimal | None = None
    updated_at: datetime


class CashCarryOpportunity(BaseSchema):
    exchange: ExchangeName
    symbol: str
    spot_price: Decimal
    perp_price: Decimal
    basis_pct: Decimal
    funding_rate_pct: Decimal
    quantity: Decimal
    spot_volume_24h_usdt: Decimal
    perp_volume_24h_usdt: Decimal
    estimated_basis_profit: Decimal
    estimated_funding_income: Decimal
    estimated_open_close_fee: Decimal
    estimated_net_profit: Decimal
    max_safe_notional_usdt: Decimal | None = None
    notional_usdt: Decimal = Decimal("0")
    margin_required_usdt: Decimal = Decimal("0")
    leverage: Decimal = Decimal("1")
    blocked_reasons: list[str] = Field(default_factory=list)
    data_source: DataSource = DataSource.MOCK
    updated_at: datetime


class AlphaCarryOpportunity(BaseSchema):
    symbol: str
    alpha_symbol: str
    alpha_trade_symbol: str
    alpha_id: str
    alpha_name: str
    chain_name: str
    contract_address: str
    perp_symbol: str
    alpha_price: Decimal
    perp_bid_price: Decimal
    perp_ask_price: Decimal
    basis_pct: Decimal
    funding_rate_pct: Decimal
    alpha_volume_24h_usdt: Decimal
    perp_volume_24h_usdt: Decimal
    notional_usdt: Decimal
    estimated_basis_profit: Decimal
    estimated_funding_income: Decimal
    estimated_fee_reserve: Decimal
    estimated_net_profit: Decimal
    blocked_reasons: list[str] = Field(default_factory=list)
    data_source: DataSource = DataSource.LIVE
    updated_at: datetime


class Mt4SpreadOpportunity(BaseSchema):
    instrument: str
    instrument_type: Literal["stock", "commodity"]
    mt4_symbol: str
    exchange: ExchangeName
    exchange_symbol: str
    long_venue: str
    short_venue: str
    mt4_bid: Decimal
    mt4_ask: Decimal
    exchange_bid: Decimal
    exchange_ask: Decimal
    spread_pct: Decimal
    notional_usdt: Decimal
    margin_required_usdt: Decimal
    leverage: Decimal
    mt4_contract_size: Decimal
    mt4_lots: Decimal
    hedge_base_quantity: Decimal
    estimated_exchange_funding_net: Decimal
    estimated_mt4_overnight_net: Decimal
    estimated_open_close_fee: Decimal
    estimated_net_profit: Decimal
    blocked_reasons: list[str] = Field(default_factory=list)
    data_source: DataSource = DataSource.LIVE
    updated_at: datetime


class TradeHistory(BaseSchema):
    trade_pair_id: str
    strategy_type: Literal["cash_carry", "mt4_spread"] = "cash_carry"
    symbol: str
    quantity: Decimal
    opened_at: datetime
    closed_at: datetime | None
    long_exchange: ExchangeName
    short_exchange: ExchangeName
    long_open_price: Decimal
    long_close_price: Decimal | None
    short_open_price: Decimal
    short_close_price: Decimal | None
    actual_fee: Decimal
    total_pnl: Decimal
    long_pnl: Decimal
    short_pnl: Decimal
    funding_net: Decimal
    actual_net_profit: Decimal
    close_reason: str | None
    long_order_ids: list[str]
    short_order_ids: list[str]
    reconcile_status: ReconcileStatus


class BotSettings(BaseSchema):
    order_notional_usdt: Decimal = Decimal("100")
    max_total_notional_usdt: Decimal = Decimal("2000")
    max_symbol_notional_usdt: Decimal = Decimal("500")
    default_leverage: Decimal = Decimal("2")
    max_leverage: Decimal = Decimal("3")
    margin_mode: Literal["isolated", "cross"] = "isolated"
    cash_carry_min_basis_pct: Decimal = Decimal("0.8")
    cash_carry_max_entry_basis_pct: Decimal = Decimal("3")
    cash_carry_min_entry_net_pct: Decimal = Decimal("0.8")
    cash_carry_close_basis_pct: Decimal = Decimal("0.2")
    cash_carry_min_funding_rate_pct: Decimal = Decimal("0")
    cash_carry_min_volume_usdt: Decimal = Decimal("300000")
    mt4_spread_enabled: bool = True
    mt4_min_spread_pct: Decimal = Decimal("0.5")
    mt4_min_net_profit_usdt: Decimal = Decimal("0.01")
    mt4_notional_usdt: Decimal = Decimal("100")
    mt4_default_leverage: Decimal = Decimal("5")
    mt4_max_quote_age_seconds: Decimal = Decimal("10")
    alpha_alert_enabled: bool = True
    alpha_alert_notional_usdt: Decimal = Decimal("100")
    alpha_alert_min_basis_pct: Decimal = Decimal("0.8")
    alpha_alert_min_funding_rate_pct: Decimal = Decimal("0")
    alpha_alert_min_volume_usdt: Decimal = Decimal("300000")
    alpha_alert_fee_reserve_pct: Decimal = Decimal("0.2")
    take_profit_usdt: Decimal = Decimal("8")
    stop_loss_usdt: Decimal = Decimal("12")
    max_slippage_pct: Decimal = Decimal("0.2")
    min_funding_net_usdt: Decimal = Decimal("0.01")
    cash_carry_recovery_exit_max_loss_usdt: Decimal = Decimal("8")
    cash_carry_max_recovery_funding_intervals: Decimal = Decimal("12")
    cash_carry_max_positions_per_exchange: int = 3
    max_add_count: int = 2
    add_notional_usdt: Decimal = Decimal("0")
    add_trigger_spread_pct: Decimal = Decimal("2.2")
    single_exchange_max_notional_usdt: Decimal = Decimal("1000")
    symbol_blacklist: list[str] = Field(default_factory=list)
    exchange_blacklist: list[ExchangeName] = Field(default_factory=list)
    cash_carry_enabled: bool = True
    cash_carry_auto_open_enabled: bool = False
    cash_carry_auto_close_enabled: bool = False
    cash_carry_auto_transfer_enabled: bool = False
    cash_carry_auto_trade_enabled: bool = False
    manual_confirm_required: bool = True
    ai_risk_monitor_enabled: bool = True
    emergency_close_enabled: bool = False

    @field_validator("max_add_count")
    @classmethod
    def validate_add_count(cls, value: int) -> int:
        if value < 0 or value > 10:
            raise ValueError("max_add_count must be between 0 and 10")
        return value

    @field_validator("cash_carry_max_positions_per_exchange")
    @classmethod
    def validate_cash_carry_slots(cls, value: int) -> int:
        if value < 1 or value > 5:
            raise ValueError("cash_carry_max_positions_per_exchange must be between 1 and 5")
        return value

    @field_validator(
        "order_notional_usdt",
        "max_total_notional_usdt",
        "max_symbol_notional_usdt",
        "default_leverage",
        "max_leverage",
        "cash_carry_min_basis_pct",
        "cash_carry_max_entry_basis_pct",
        "cash_carry_min_entry_net_pct",
        "cash_carry_close_basis_pct",
        "cash_carry_min_funding_rate_pct",
        "cash_carry_min_volume_usdt",
        "mt4_min_spread_pct",
        "mt4_min_net_profit_usdt",
        "mt4_notional_usdt",
        "mt4_default_leverage",
        "mt4_max_quote_age_seconds",
        "alpha_alert_notional_usdt",
        "alpha_alert_min_basis_pct",
        "alpha_alert_min_funding_rate_pct",
        "alpha_alert_min_volume_usdt",
        "alpha_alert_fee_reserve_pct",
        "take_profit_usdt",
        "stop_loss_usdt",
        "max_slippage_pct",
        "min_funding_net_usdt",
        "cash_carry_recovery_exit_max_loss_usdt",
        "cash_carry_max_recovery_funding_intervals",
        "add_notional_usdt",
        "add_trigger_spread_pct",
        "single_exchange_max_notional_usdt",
    )
    @classmethod
    def validate_positive_decimal(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("numeric settings must not be negative")
        return value

    @model_validator(mode="after")
    def default_add_notional(self):
        if self.add_notional_usdt <= 0:
            self.add_notional_usdt = self.order_notional_usdt
        return self


class RiskEvent(BaseSchema):
    id: str
    severity: Literal["info", "warning", "critical"]
    title: str
    detail: str
    action: str
    created_at: datetime


class ExchangeCredentialStatus(BaseSchema):
    exchange: ExchangeName
    configured: bool
    missing_fields: list[str]
    live_data_enabled: bool
    trading_enabled: bool
    order_execution_enabled: bool
    read_only_mode: bool
    source: Literal["vault", "env", "mixed", "missing"] = "missing"
    masked_api_key: str | None = None
    updated_at: datetime | None = None
    last_test_ok: bool | None = None
    last_test_message: str | None = None
    last_test_at: datetime | None = None
    use_testnet: bool = False
    use_demo: bool = False


class ExchangeCredentialInput(BaseSchema):
    api_key: str | None = None
    api_secret: str | None = None
    passphrase: str | None = None
    use_testnet: bool | None = None
    use_demo: bool | None = None


class DeepSeekCredentialInput(BaseSchema):
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None


class Mt4CredentialInput(BaseSchema):
    bridge_token: str | None = None


class AIInsight(BaseSchema):
    provider: str
    model: str
    status: Literal["ready", "disabled", "not_configured", "error"]
    content: str
    updated_at: datetime
    next_refresh_at: datetime | None = None


class RealtimeSnapshot(BaseSchema):
    balances: list[ExchangeBalance]
    positions: list[PositionSnapshot]
    cash_carry_opportunities: list[CashCarryOpportunity]
    cash_carry_candidates: list[CashCarryOpportunity]
    cash_carry_positions: list[CashCarryPositionRow]
    alpha_alert_opportunities: list[AlphaCarryOpportunity]
    alpha_alert_candidates: list[AlphaCarryOpportunity]
    mt4_spread_opportunities: list[Mt4SpreadOpportunity]
    mt4_spread_candidates: list[Mt4SpreadOpportunity]
    trades: list[TradeHistory]
    settings: BotSettings
    risk_events: list[RiskEvent]
    credential_status: list[ExchangeCredentialStatus]
    ai_insight: AIInsight
    data_source: DataSource = DataSource.MOCK
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
