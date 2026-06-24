from decimal import Decimal

from app.models import MarketQuote
from app.mt4_costs import live_spread_usd_per_oz, slippage_budget_usd_per_oz, spread_cost_usd


def test_live_mt4_spread_cost_uses_bid_ask_difference():
    quote = MarketQuote(symbol="XAUUSD", bid=Decimal("4078.46"), ask=Decimal("4078.77"))

    assert live_spread_usd_per_oz(quote) == Decimal("0.31")
    assert spread_cost_usd(quote, Decimal("2")) == Decimal("0.62")


def test_slippage_budget_includes_realtime_mt4_spread():
    quote = MarketQuote(symbol="XAUUSD", bid=Decimal("3999.8"), ask=Decimal("4000.0"))

    assert slippage_budget_usd_per_oz(30, Decimal("0.01"), quote) == Decimal("0.5")
