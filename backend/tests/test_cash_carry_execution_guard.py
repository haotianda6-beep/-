from decimal import Decimal

from app.services.cash_carry_execution_guard import forward_close_depth_guard, forward_open_depth_guard


def test_forward_open_depth_guard_blocks_when_vwap_basis_is_too_low() -> None:
    result = forward_open_depth_guard(
        _DepthExchange(asks=[[100, 0.5], [101, 10]], bids=[[99, 10]]),
        _DepthExchange(asks=[[102, 10]], bids=[[100.5, 0.5], [100.1, 10]]),
        "AIA/USDT",
        "AIA/USDT:USDT",
        Decimal("100"),
        Decimal("0.8"),
    )

    assert not result.ok
    assert "深度均价开仓基差" in result.reason


def test_forward_open_depth_guard_blocks_when_stable_net_is_too_low() -> None:
    result = forward_open_depth_guard(
        _DepthExchange(asks=[[100, 10]], bids=[[99, 10]]),
        _DepthExchange(asks=[[102, 10]], bids=[[101, 10]]),
        "AIA/USDT",
        "AIA/USDT:USDT",
        Decimal("100"),
        Decimal("0.8"),
        min_net_profit=Decimal("1.00"),
        open_close_fee=Decimal("0.50"),
        funding_income=Decimal("0.01"),
        close_basis_pct=Decimal("0.2"),
    )

    assert not result.ok
    assert result.estimated_net_profit == Decimal("0.310")
    assert "稳定开仓安全垫" in result.reason


def test_forward_close_depth_guard_blocks_when_executable_net_would_be_loss() -> None:
    result = forward_close_depth_guard(
        _DepthExchange(asks=[[0.0062, 20000]], bids=[[0.006087, 20000]]),
        _DepthExchange(asks=[[0.006121, 20000]], bids=[[0.0060, 20000]]),
        "JCT/USDT",
        "JCT/USDT:USDT",
        Decimal("15872"),
        Decimal("15872"),
        Decimal("0.006287635090102"),
        Decimal("0.006305771169355"),
        Decimal("0.0006"),
        Decimal("0.2"),
    )

    assert not result.ok
    assert result.estimated_net_profit < 0
    assert "盘口可成交净利" in result.reason


class _DepthExchange:
    def __init__(self, asks, bids) -> None:
        self.asks = asks
        self.bids = bids

    def fetch_order_book(self, symbol, limit=20):
        return {"asks": self.asks, "bids": self.bids}

    def load_markets(self):
        return None

    def market(self, symbol):
        return {"contractSize": "1"}
