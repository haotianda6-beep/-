from dataclasses import dataclass
from decimal import Decimal

from app.core.models import ExchangeName


@dataclass(frozen=True)
class MarketQuote:
    bid: Decimal
    ask: Decimal
    funding_rate: Decimal
    volume_24h_usdt: Decimal
    depth_usdt: Decimal


BASE_QUOTES: dict[ExchangeName, dict[str, MarketQuote]] = {
    ExchangeName.BINANCE: {
        "BTCUSDT": MarketQuote(Decimal("69980"), Decimal("70000"), Decimal("0.00010"), Decimal("420000000"), Decimal("1800000")),
        "ETHUSDT": MarketQuote(Decimal("3488"), Decimal("3490"), Decimal("0.00008"), Decimal("210000000"), Decimal("980000")),
        "SOLUSDT": MarketQuote(Decimal("158.1"), Decimal("158.2"), Decimal("0.00018"), Decimal("56000000"), Decimal("220000")),
    },
    ExchangeName.OKX: {
        "BTCUSDT": MarketQuote(Decimal("71100"), Decimal("71130"), Decimal("0.00035"), Decimal("390000000"), Decimal("1700000")),
        "ETHUSDT": MarketQuote(Decimal("3502"), Decimal("3504"), Decimal("0.00005"), Decimal("190000000"), Decimal("900000")),
        "SOLUSDT": MarketQuote(Decimal("160.4"), Decimal("160.5"), Decimal("-0.00004"), Decimal("47000000"), Decimal("260000")),
    },
    ExchangeName.GATE: {
        "BTCUSDT": MarketQuote(Decimal("70420"), Decimal("70450"), Decimal("0.00012"), Decimal("86000000"), Decimal("720000")),
        "ETHUSDT": MarketQuote(Decimal("3552"), Decimal("3554"), Decimal("0.00026"), Decimal("58000000"), Decimal("610000")),
        "DOGEUSDT": MarketQuote(Decimal("0.147"), Decimal("0.1472"), Decimal("0.00030"), Decimal("180000"), Decimal("50000")),
    },
    ExchangeName.BITGET: {
        "BTCUSDT": MarketQuote(Decimal("70150"), Decimal("70180"), Decimal("0.00009"), Decimal("120000000"), Decimal("810000")),
        "ETHUSDT": MarketQuote(Decimal("3494"), Decimal("3496"), Decimal("0.00010"), Decimal("92000000"), Decimal("700000")),
        "DOGEUSDT": MarketQuote(Decimal("0.151"), Decimal("0.1512"), Decimal("0.00042"), Decimal("760000"), Decimal("80000")),
    },
    ExchangeName.BYBIT: {
        "BTCUSDT": MarketQuote(Decimal("70040"), Decimal("70070"), Decimal("0.00011"), Decimal("310000000"), Decimal("1300000")),
        "ETHUSDT": MarketQuote(Decimal("3560"), Decimal("3562"), Decimal("0.00031"), Decimal("150000000"), Decimal("840000")),
        "SOLUSDT": MarketQuote(Decimal("157.8"), Decimal("157.9"), Decimal("0.00007"), Decimal("64000000"), Decimal("300000")),
    },
}

INTEROPERABLE_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT"}
