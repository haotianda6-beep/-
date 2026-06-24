from decimal import Decimal

from app.main import _dynamic_close_spread, _immediate_close_net, _projected_close_net_after_next_settlement


def test_immediate_close_net_excludes_future_funding_and_swap():
    immediate = _immediate_close_net(
        gross=Decimal("1.00"),
        fees=Decimal("0.10"),
        accrued_funding=Decimal("0.20"),
        accrued_swap=Decimal("-0.05"),
        mt4_spread_protection=Decimal("0.30"),
    )

    assert immediate == Decimal("0.75")
    assert _projected_close_net_after_next_settlement(
        immediate,
        funding_estimate=Decimal("0.40"),
        mt4_swap_estimate=Decimal("-0.60"),
    ) == Decimal("0.55")


def test_dynamic_close_spread_does_not_go_negative_when_buffer_is_large():
    assert _dynamic_close_spread(
        profitable_spread_threshold=Decimal("2.52"),
        exit_follow_buffer=Decimal("2.70"),
        close_profit=Decimal("0.20"),
    ) == Decimal("0")
