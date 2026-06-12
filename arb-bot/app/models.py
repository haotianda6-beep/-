from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


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


class Mt4Position(BaseModel):
    ticket: int
    symbol: str
    side: Side
    lots: Decimal
    open_price: Decimal


class Mt4Tick(BaseModel):
    token: str | None = None
    symbol: str
    bid: Decimal
    ask: Decimal
    timestamp_ms: int = Field(default_factory=utc_now_ms)
    positions: list[Mt4Position] = Field(default_factory=list)


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


class EngineStatus(BaseModel):
    state: StrategyState
    live_trading: bool
    paper_mode: bool
    binance_connected: bool
    mt4_connected: bool
    binance_symbol: str
    mt4_symbol: str
    maker_fee_rate: Decimal | None = None
    binance_quote: MarketQuote | None = None
    mt4_quote: MarketQuote | None = None
    open_pair: OpenPair | None = None
    last_error: str | None = None

