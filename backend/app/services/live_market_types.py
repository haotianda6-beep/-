from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.core.models import CashCarryOpportunity, ExchangeName
from app.services.asset_identity import MarketAsset
from app.services.cash_carry_scope import CASH_CARRY_EXCHANGES, CASH_CARRY_EXCHANGE_SET
from app.services.market_checks import TransferNetworks


SWAP_EXCHANGE_IDS = {
    ExchangeName.BINANCE: "binanceusdm",
    ExchangeName.OKX: "okx",
    ExchangeName.GATE: "gateio",
    ExchangeName.BITGET: "bitget",
    ExchangeName.BYBIT: "bybit",
}

SPOT_EXCHANGE_IDS = {
    ExchangeName.BINANCE: "binance",
    ExchangeName.OKX: "okx",
    ExchangeName.GATE: "gateio",
    ExchangeName.BITGET: "bitget",
    ExchangeName.BYBIT: "bybit",
}

@dataclass
class SwapMarket:
    symbol: str
    ccxt_symbol: str
    taker_fee: Decimal
    asset: MarketAsset


@dataclass
class ExchangeMarketData:
    exchange: ExchangeName
    swap_exchange: Any | None = None
    swaps: dict[str, SwapMarket] = field(default_factory=dict)
    spot_markets: dict[str, MarketAsset] = field(default_factory=dict)
    transfer_networks: dict[str, TransferNetworks] = field(default_factory=dict)
    transfer_query_ok: bool = False
    tickers: dict[str, dict[str, Any]] = field(default_factory=dict)
    funding_rates: dict[str, Decimal] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)


@dataclass
class CashCarryScan:
    opportunities: list[CashCarryOpportunity] = field(default_factory=list)
    candidates: list[CashCarryOpportunity] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
