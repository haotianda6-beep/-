from decimal import Decimal


def calculate_current_net_profit(
    long_unrealized_pnl: Decimal,
    short_unrealized_pnl: Decimal,
    open_fee: Decimal,
    estimated_close_fee: Decimal,
    realized_funding_net: Decimal,
) -> Decimal:
    return (
        long_unrealized_pnl
        + short_unrealized_pnl
        - open_fee
        - estimated_close_fee
        + realized_funding_net
    )


def calculate_spread_pct(long_price: Decimal, short_price: Decimal) -> Decimal:
    if long_price <= 0:
        raise ValueError("long_price must be positive")
    return (short_price - long_price) / long_price * Decimal("100")


def calculate_funding_net(
    notional_usdt: Decimal,
    long_funding_rate: Decimal,
    short_funding_rate: Decimal,
) -> Decimal:
    return notional_usdt * (short_funding_rate - long_funding_rate)

