from decimal import Decimal

from app.models import HistoryBar, MarketQuote
from app.mt4_costs import live_spread_usd_per_oz, recent_move_budget_usd_per_oz, slippage_budget_usd_per_oz, spread_cost_usd


def test_live_mt4_spread_cost_uses_bid_ask_difference():
    quote = MarketQuote(symbol="XAUUSD", bid=Decimal("4078.46"), ask=Decimal("4078.77"))

    assert live_spread_usd_per_oz(quote) == Decimal("0.31")
    assert spread_cost_usd(quote, Decimal("2")) == Decimal("0.62")


def test_slippage_budget_includes_realtime_mt4_spread():
    quote = MarketQuote(symbol="XAUUSD", bid=Decimal("3999.8"), ask=Decimal("4000.0"))

    assert slippage_budget_usd_per_oz(30, Decimal("0.01"), quote) == Decimal("0.5")


def test_recent_move_budget_uses_percentile_when_enough_bars():
    closes = [
        Decimal("4000.0"),
        Decimal("4000.2"),
        Decimal("4001.2"),
        Decimal("4001.5"),
        Decimal("4003.5"),
        Decimal("4004.0"),
        Decimal("4004.8"),
        Decimal("4006.3"),
        Decimal("4006.5"),
        Decimal("4007.7"),
    ]
    bars = [
        HistoryBar(open_time_ms=index * 60_000, open=close, high=close, low=close, close=close)
        for index, close in enumerate(closes)
    ]

    assert recent_move_budget_usd_per_oz(bars, percentile=70, min_points=8) == Decimal("1.0")


def test_recent_move_budget_is_zero_when_sample_is_short():
    bars = [
        HistoryBar(open_time_ms=0, open=Decimal("4000"), high=Decimal("4000"), low=Decimal("4000"), close=Decimal("4000")),
        HistoryBar(open_time_ms=60_000, open=Decimal("4001"), high=Decimal("4001"), low=Decimal("4001"), close=Decimal("4001")),
    ]

    assert recent_move_budget_usd_per_oz(bars, percentile=70, min_points=8) == Decimal("0")
