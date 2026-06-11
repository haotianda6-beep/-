from dataclasses import dataclass, field

from app.core.models import CashCarryOpportunity, ExchangeName


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
class CashCarryScan:
    opportunities: list[CashCarryOpportunity] = field(default_factory=list)
    candidates: list[CashCarryOpportunity] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
