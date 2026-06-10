from decimal import Decimal

from app.core.pnl import (
    calculate_current_net_profit,
    calculate_funding_net,
    calculate_spread_pct,
)


def test_current_net_profit_uses_required_formula() -> None:
    result = calculate_current_net_profit(
        long_unrealized_pnl=Decimal("5"),
        short_unrealized_pnl=Decimal("-1"),
        open_fee=Decimal("0.4"),
        estimated_close_fee=Decimal("0.3"),
        realized_funding_net=Decimal("0.2"),
    )

    assert result == Decimal("3.5")


def test_spread_pct_uses_long_ask_and_short_bid() -> None:
    result = calculate_spread_pct(Decimal("100"), Decimal("102"))

    assert result == Decimal("2.00")


def test_funding_net_is_positive_when_short_rate_exceeds_long_rate() -> None:
    result = calculate_funding_net(
        notional_usdt=Decimal("100"),
        long_funding_rate=Decimal("0.0001"),
        short_funding_rate=Decimal("0.0004"),
    )

    assert result == Decimal("0.0300")

