from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class StrategyState(str, Enum):
    IDLE = "IDLE"
    QUOTING_BINANCE_ENTRY = "QUOTING_BINANCE_ENTRY"
    HEDGING_MT4 = "HEDGING_MT4"
    PAIR_OPEN = "PAIR_OPEN"
    QUOTING_BINANCE_EXIT = "QUOTING_BINANCE_EXIT"
    CLOSING_MT4 = "CLOSING_MT4"
    UNHEDGED = "UNHEDGED"
    EMERGENCY_CLOSE_BINANCE = "EMERGENCY_CLOSE_BINANCE"
    PAUSED = "PAUSED"


class PairDirection(str, Enum):
    BINANCE_SHORT_MT4_LONG = "BINANCE_SHORT_MT4_LONG"
    BINANCE_LONG_MT4_SHORT = "BINANCE_LONG_MT4_SHORT"


class MarketQuote(BaseModel):
    symbol: str
    bid: Decimal
    ask: Decimal
    timestamp_ms: int = Field(default_factory=utc_now_ms)

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")


class ExchangeFilters(BaseModel):
    tick_size: Decimal
    qty_step: Decimal
    min_qty: Decimal = Decimal("0")


class EntryPlan(BaseModel):
    direction: PairDirection
    binance_side: Side
    limit_price: Decimal
    quantity_oz: Decimal
    edge: Decimal
    mt4_hedge_side: Side
    mt4_price_limit: Decimal


class OrderRequest(BaseModel):
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal | None = None
    client_order_id: str = Field(default_factory=lambda: f"arb_{uuid4().hex[:24]}")
    post_only: bool = True
    reduce_only: bool = False
    position_side: str | None = None


class OrderUpdate(BaseModel):
    order_id: str
    client_order_id: str
    symbol: str
    side: Side
    status: OrderStatus
    price: Decimal
    orig_qty: Decimal
    executed_qty: Decimal = Decimal("0")
    avg_price: Decimal = Decimal("0")
    is_maker: bool = True
    reduce_only: bool = False
    message: str | None = None
    timestamp_ms: int = Field(default_factory=utc_now_ms)


class BinanceFundingInfo(BaseModel):
    symbol: str
    funding_rate: Decimal
    next_funding_time_ms: int
    mark_price: Decimal | None = None
    timestamp_ms: int = Field(default_factory=utc_now_ms)


class Mt4Position(BaseModel):
    ticket: int
    symbol: str
    side: Side
    lots: Decimal
    open_price: Decimal
    profit: Decimal = Decimal("0")
    swap: Decimal = Decimal("0")


class Mt4SwapInfo(BaseModel):
    swap_long_per_lot: Decimal | None = None
    swap_short_per_lot: Decimal | None = None
    swap_type: int | None = None
    tick_value: Decimal | None = None
    tick_size: Decimal | None = None
    point: Decimal | None = None
    next_rollover_time_ms: int | None = None


class Mt4Tick(BaseModel):
    token: str | None = None
    symbol: str
    bid: Decimal
    ask: Decimal
    timestamp_ms: int = Field(default_factory=utc_now_ms)
    positions: list[Mt4Position] = Field(default_factory=list)
    swap_long_per_lot: Decimal | None = None
    swap_short_per_lot: Decimal | None = None
    swap_type: int | None = None
    tick_value: Decimal | None = None
    tick_size: Decimal | None = None
    point: Decimal | None = None
    next_rollover_time_ms: int | None = None


class Mt4Command(BaseModel):
    command_id: str = Field(default_factory=lambda: f"mt4_{uuid4().hex[:24]}")
    action: Literal["BUY", "SELL", "CLOSE"]
    symbol: str
    lots: Decimal
    slippage_points: int
    max_price: Decimal | None = None
    min_price: Decimal | None = None
    ticket: int | None = None
    reason: str
    created_ms: int = Field(default_factory=utc_now_ms)


class Mt4Report(BaseModel):
    token: str | None = None
    command_id: str
    status: Literal["ok", "error"]
    action: str
    ticket: int | None = None
    fill_price: Decimal | None = None
    lots: Decimal = Decimal("0")
    error_code: int | None = None
    message: str | None = None
    timestamp_ms: int = Field(default_factory=utc_now_ms)


class HistoryBar(BaseModel):
    open_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None


class Mt4HistoryPayload(BaseModel):
    token: str | None = None
    symbol: str
    interval: Literal["1m", "5m", "15m", "1h"] = "1m"
    bars: list[HistoryBar] = Field(default_factory=list)


class SpreadAnalysisPoint(BaseModel):
    timestamp_ms: int
    mt4_close: Decimal
    binance_close: Decimal
    diff: Decimal
    abs_diff: Decimal


class SpreadAnalysis(BaseModel):
    ready: bool
    reason: str | None = None
    days: int
    interval: str
    threshold: Decimal
    mt4_bars: int
    binance_bars: int
    matched_points: int
    returned_to_threshold: bool
    return_count: int
    min_abs_diff: Decimal | None = None
    min_abs_diff_time_ms: int | None = None
    latest_diff: Decimal | None = None
    latest_time_ms: int | None = None
    closest_points: list[SpreadAnalysisPoint] = Field(default_factory=list)
    latest_points: list[SpreadAnalysisPoint] = Field(default_factory=list)


class OpenPair(BaseModel):
    pair_id: str = Field(default_factory=lambda: f"pair_{uuid4().hex[:24]}")
    direction: PairDirection
    quantity_oz: Decimal
    binance_entry_price: Decimal
    mt4_entry_price: Decimal
    binance_order_id: str
    mt4_ticket: int | None = None
    opened_ms: int = Field(default_factory=utc_now_ms)
    realized_pnl: Decimal = Decimal("0")


class PositionMetrics(BaseModel):
    binance_funding_rate: Decimal | None = None
    binance_next_funding_time_ms: int | None = None
    binance_funding_estimate: Decimal | None = None
    mt4_next_rollover_time_ms: int | None = None
    mt4_swap_estimate: Decimal | None = None
    mt4_accrued_swap: Decimal | None = None
    estimated_close_gross: Decimal | None = None
    estimated_fees: Decimal | None = None
    estimated_close_net: Decimal | None = None


class ExecutionPlanStatus(BaseModel):
    summary: str
    binance_order_side: Side | None = None
    binance_order_price: Decimal | None = None
    binance_order_qty: Decimal | None = None
    mt4_follow_side: Side | None = None
    mt4_price_limit: Decimal | None = None
    max_follow_seconds: Decimal


class RuntimeConfig(BaseModel):
    binance_api_configured: bool
    config_files: list[str]
    mt4_script_path: str
    binance_leverage: int
    binance_entry_offset_usd: Decimal
    open_min_edge: Decimal
    close_max_spread: Decimal
    min_locked_edge: Decimal
    max_order_age_ms: int
    max_quote_age_ms: int
    max_hedge_delay_ms: int
    max_unhedged_loss_usd_per_oz: Decimal
    daily_loss_limit_usdt: Decimal
    target_oz: Decimal
    mt4_lot_size_oz: Decimal
    mt4_slippage_points: int
    loop_interval_ms: int
    paper_auto_fill: bool
    paper_fill_delay_ms: int


class RuntimeConfigUpdate(BaseModel):
    binance_leverage: int | None = None
    binance_entry_offset_usd: Decimal | None = None
    open_min_edge: Decimal | None = None
    close_max_spread: Decimal | None = None
    min_locked_edge: Decimal | None = None
    max_order_age_ms: int | None = None
    max_quote_age_ms: int | None = None
    max_hedge_delay_ms: int | None = None
    max_unhedged_loss_usd_per_oz: Decimal | None = None
    daily_loss_limit_usdt: Decimal | None = None
    target_oz: Decimal | None = None
    mt4_lot_size_oz: Decimal | None = None
    mt4_slippage_points: int | None = None
    loop_interval_ms: int | None = None
    paper_auto_fill: bool | None = None
    paper_fill_delay_ms: int | None = None

    @field_validator(
        "open_min_edge",
        "close_max_spread",
        "min_locked_edge",
        "max_unhedged_loss_usd_per_oz",
        "daily_loss_limit_usdt",
        "target_oz",
        "mt4_lot_size_oz",
        "binance_entry_offset_usd",
    )
    @classmethod
    def positive_decimal(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value <= 0:
            raise ValueError("必须大于 0")
        return value

    @field_validator(
        "max_order_age_ms",
        "max_quote_age_ms",
        "max_hedge_delay_ms",
        "loop_interval_ms",
        "paper_fill_delay_ms",
    )
    @classmethod
    def positive_int(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("必须大于 0")
        return value

    @field_validator("mt4_slippage_points")
    @classmethod
    def non_negative_int(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("不能小于 0")
        return value

    @field_validator("binance_leverage")
    @classmethod
    def valid_leverage(cls, value: int | None) -> int | None:
        if value is not None and (value < 1 or value > 125):
            raise ValueError("杠杆必须在 1 到 125 之间")
        return value


class EngineStatus(BaseModel):
    state: StrategyState
    live_trading: bool
    paper_mode: bool
    binance_connected: bool
    mt4_connected: bool
    binance_symbol: str
    mt4_symbol: str
    maker_fee_rate: Decimal | None = None
    binance_funding: BinanceFundingInfo | None = None
    binance_quote: MarketQuote | None = None
    mt4_quote: MarketQuote | None = None
    open_pair: OpenPair | None = None
    position_metrics: PositionMetrics | None = None
    execution_plan: ExecutionPlanStatus
    last_error: str | None = None
    config: RuntimeConfig
