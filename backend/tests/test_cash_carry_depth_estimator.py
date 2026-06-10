from decimal import Decimal

from app.core.models import BotSettings
from app.services.cash_carry_depth_estimator import estimate_max_safe_notional


def test_forward_max_safe_notional_is_capped_by_vwap_slippage() -> None:
    settings = BotSettings(
        order_notional_usdt=Decimal("100"),
        max_slippage_pct=Decimal("0.5"),
        cash_carry_min_basis_pct=Decimal("0.8"),
        min_funding_net_usdt=Decimal("0.01"),
    )

    result = estimate_max_safe_notional(
        _BookExchange(asks=[[100, 1], [101, 1]], bids=[[99, 2]]),
        _BookExchange(asks=[[103, 2]], bids=[[102, 1], [100, 1]]),
        "ABC/USDT",
        "ABC/USDT:USDT",
        "forward",
        settings,
        Decimal("0.0005"),
        Decimal("0.0005"),
        Decimal("0.0002"),
    )

    assert result is not None
    assert Decimal("100") <= result < Decimal("150")


def test_reverse_max_safe_notional_is_capped_by_borrow_available() -> None:
    settings = BotSettings(
        order_notional_usdt=Decimal("20"),
        max_slippage_pct=Decimal("0.5"),
        reverse_cash_carry_min_discount_pct=Decimal("0.8"),
        min_funding_net_usdt=Decimal("0.01"),
    )

    result = estimate_max_safe_notional(
        _BookExchange(asks=[[101, 2]], bids=[[100, 2]]),
        _BookExchange(asks=[[98, 2]], bids=[[97, 2]]),
        "ABC/USDT",
        "ABC/USDT:USDT",
        "reverse",
        settings,
        Decimal("0.0005"),
        Decimal("0.0005"),
        Decimal("-0.0002"),
        borrow_cost_rate=Decimal("0.00001"),
        borrow_available_qty=Decimal("0.5"),
    )

    assert result is not None
    assert Decimal("49.99") <= result <= Decimal("50.01")


class _BookExchange:
    def __init__(self, asks, bids) -> None:
        self.asks = asks
        self.bids = bids

    def fetch_order_book(self, symbol, limit=50):
        return {"asks": self.asks, "bids": self.bids}

    def load_markets(self):
        return None

    def market(self, symbol):
        return {"contractSize": "1"}
