from __future__ import annotations

from decimal import Decimal

from app.models import MarketQuote


def live_spread_usd_per_oz(quote: MarketQuote | None) -> Decimal | None:
    if not quote:
        return None
    spread = quote.ask - quote.bid
    return spread if spread > 0 else Decimal("0")


def spread_cost_usd(quote: MarketQuote | None, quantity_oz: Decimal) -> Decimal | None:
    spread = live_spread_usd_per_oz(quote)
    if spread is None:
        return None
    return spread * quantity_oz


def slippage_budget_usd_per_oz(slippage_points: int, point: Decimal, quote: MarketQuote | None) -> Decimal:
    base = Decimal(slippage_points) * point
    spread = live_spread_usd_per_oz(quote)
    return base + (spread or Decimal("0"))
