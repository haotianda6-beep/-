from dataclasses import dataclass
from decimal import Decimal

from app.core.models import BotSettings


@dataclass(frozen=True)
class CashCarryCloseDecision:
    should_close: bool
    reason: str


def cash_carry_close_decision(
    current_net_profit: Decimal,
    basis_pct: Decimal,
    funding_rate_pct: Decimal,
    settings: BotSettings,
    *,
    has_live_net: bool,
) -> CashCarryCloseDecision:
    if has_live_net and settings.take_profit_usdt > 0 and current_net_profit >= settings.take_profit_usdt:
        return CashCarryCloseDecision(True, f"固定U止盈达到 {current_net_profit} USDT")
    if basis_pct > settings.cash_carry_close_basis_pct:
        return CashCarryCloseDecision(False, "基差尚未达到收敛平仓线")
    if not has_live_net:
        return CashCarryCloseDecision(True, f"基差收敛到 {basis_pct}%")
    if current_net_profit > 0:
        return CashCarryCloseDecision(True, f"基差收敛到 {basis_pct}%，执行前净利估算 {current_net_profit} USDT")
    if _has_recovery_potential(current_net_profit, funding_rate_pct, settings):
        return CashCarryCloseDecision(False, f"基差已收敛但当前仍亏损 {current_net_profit} USDT，资金费率仍有恢复空间")
    return CashCarryCloseDecision(True, f"基差已收敛但当前亏损 {current_net_profit} USDT，且资金费率恢复空间不足")


def _has_recovery_potential(current_net_profit: Decimal, funding_rate_pct: Decimal, settings: BotSettings) -> bool:
    if funding_rate_pct <= settings.cash_carry_min_funding_rate_pct:
        return False
    if settings.stop_loss_usdt > 0 and current_net_profit <= -settings.stop_loss_usdt:
        return False
    return True
