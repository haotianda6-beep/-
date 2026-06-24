from __future__ import annotations

from decimal import Decimal

from app.models import MarketQuote


MAX_REASONABLE_XAU_MID_GAP = Decimal("100")


def xau_quote_gap_reason(
    binance_quote: MarketQuote | None,
    mt4_quote: MarketQuote | None,
    max_gap: Decimal = MAX_REASONABLE_XAU_MID_GAP,
) -> str | None:
    if not binance_quote or not mt4_quote:
        return None
    invalid = _invalid_quote_reason("币安", binance_quote) or _invalid_quote_reason("MT4", mt4_quote)
    if invalid:
        return invalid
    binance_mid = (binance_quote.bid + binance_quote.ask) / Decimal("2")
    mt4_mid = (mt4_quote.bid + mt4_quote.ask) / Decimal("2")
    gap = abs(binance_mid - mt4_mid)
    if gap > max_gap:
        return f"币安与MT4中间价相差 {gap:.2f} 美元，超过 {max_gap} 美元，疑似错品种或坏报价"
    return None


def _invalid_quote_reason(label: str, quote: MarketQuote) -> str | None:
    if quote.bid <= 0 or quote.ask <= 0:
        return f"{label}报价为0或负数"
    if quote.ask < quote.bid:
        return f"{label}卖价小于买价"
    return None
