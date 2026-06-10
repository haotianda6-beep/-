from decimal import Decimal, ROUND_HALF_UP

from app.core.models import ExchangeName


FEE_RATES = {
    ExchangeName.BINANCE: Decimal("0.0004"),
    ExchangeName.OKX: Decimal("0.0005"),
    ExchangeName.GATE: Decimal("0.0006"),
    ExchangeName.BITGET: Decimal("0.0006"),
    ExchangeName.BYBIT: Decimal("0.00055"),
}


def q(value: Decimal, places: str = "0.0001") -> Decimal:
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)

