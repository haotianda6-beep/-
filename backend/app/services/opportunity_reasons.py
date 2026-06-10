from decimal import Decimal

from app.core.market_math import q
from app.core.models import BotSettings


def base_filter_reasons(
    spread_pct: Decimal,
    funding_net: Decimal,
    min_volume: Decimal,
    spot_market_ok: bool,
    settings: BotSettings,
) -> list[str]:
    reasons: list[str] = []
    if spread_pct < settings.min_open_spread_pct:
        reasons.append(f"价差未达开仓阈值 {q(spread_pct)}% < {settings.min_open_spread_pct}%")
    if funding_net < settings.min_funding_net_usdt:
        reasons.append(f"资金费率净收益不足 {q(funding_net)}U < {settings.min_funding_net_usdt}U")
    if min_volume < settings.min_24h_volume_usdt:
        reasons.append(f"最低24h成交量不足 {q(min_volume, '0.01')}U < {settings.min_24h_volume_usdt}U")
    if not spot_market_ok:
        reasons.append("双方现货市场未同时存在，不属于链路未确认")
    return reasons
