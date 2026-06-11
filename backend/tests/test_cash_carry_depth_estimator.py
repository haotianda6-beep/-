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
        settings,
        Decimal("0.0005"),
        Decimal("0.0005"),
        Decimal("0.0002"),
    )

    assert result is not None
    assert Decimal("100") <= result < Decimal("150")


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
