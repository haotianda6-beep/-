from decimal import Decimal

from app.core.market_math import q
from app.core.models import BotSettings


ENTRY_NET_FLOOR_PCT = Decimal("0.5")


def entry_net_floor(settings: BotSettings) -> Decimal:
    pct_floor = settings.order_notional_usdt * ENTRY_NET_FLOOR_PCT / Decimal("100")
    return max(settings.min_funding_net_usdt, pct_floor)


def convergence_basis_profit(settings: BotSettings, basis_pct: Decimal) -> Decimal:
    tradable_basis = max(Decimal("0"), basis_pct - settings.cash_carry_close_basis_pct)
    return settings.order_notional_usdt * tradable_basis / Decimal("100")


def estimated_entry_net_profit(
    settings: BotSettings,
    basis_pct: Decimal,
    funding_rate: Decimal,
    open_close_fee: Decimal,
) -> Decimal:
    return convergence_basis_profit(settings, basis_pct) + settings.order_notional_usdt * funding_rate - open_close_fee


def entry_quality_reasons(estimated_net_profit: Decimal, settings: BotSettings) -> list[str]:
    floor = entry_net_floor(settings)
    if estimated_net_profit >= floor:
        return []
    return [f"回归到平仓线后的净利预估 {q(estimated_net_profit)}U < 稳定开仓安全垫 {q(floor)}U"]
