from decimal import Decimal
from typing import Any

from app.services.live_read import decimal_from


def normalized_market_symbol(market: dict[str, Any]) -> str:
    return f"{market.get('base', '')}{market.get('quote', '')}".upper()


def normalize_ccxt_symbol(symbol: str) -> str:
    if "/" not in symbol:
        return symbol.replace("_", "").upper()
    base, rest = symbol.split("/", 1)
    quote = rest.split(":", 1)[0]
    return f"{base}{quote}".upper()


def quote_volume(ticker: dict[str, Any]) -> Decimal:
    quote = decimal_from(ticker.get("quoteVolume"))
    if quote > 0:
        return quote
    return decimal_from(ticker.get("baseVolume")) * decimal_from(ticker.get("last"))

