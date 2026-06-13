from dataclasses import dataclass
from decimal import Decimal

from app.core.models import BotSettings, CashCarryOpportunity
from app.services.cash_carry_execution_models import CashCarryPosition


@dataclass(frozen=True)
class CashCarryAddDecision:
    should_add: bool
    reason: str
    trigger_basis_pct: Decimal
    current_notional: Decimal
    next_notional: Decimal


def cash_carry_add_decision(
    record: CashCarryPosition,
    current: CashCarryOpportunity,
    settings: BotSettings,
    total_notional_usdt: Decimal,
) -> CashCarryAddDecision:
    current_notional = record.quantity * current.spot_price
    add_notional = settings.add_notional_usdt
    next_notional = current_notional + add_notional
    trigger_basis = _next_trigger_basis(record, settings)
    if record.add_count >= settings.max_add_count:
        return _no("已达到最大补仓次数", trigger_basis, current_notional, next_notional)
    if current.blocked_reasons:
        return _no("当前候选不满足正向期现开仓条件", trigger_basis, current_notional, next_notional)
    if current.basis_pct < trigger_basis:
        return _no(f"基差未走扩到补仓触发 {trigger_basis}%", trigger_basis, current_notional, next_notional)
    if next_notional > settings.max_symbol_notional_usdt:
        return _no("补仓后超过单币最大仓位", trigger_basis, current_notional, next_notional)
    if next_notional > settings.single_exchange_max_notional_usdt:
        return _no("补仓后超过单所最大暴露", trigger_basis, current_notional, next_notional)
    if total_notional_usdt + add_notional > settings.max_total_notional_usdt:
        return _no("补仓后超过最大总仓位", trigger_basis, current_notional, next_notional)
    return CashCarryAddDecision(True, "基差继续走扩，触发正向期现补仓", trigger_basis, current_notional, next_notional)


def _next_trigger_basis(record: CashCarryPosition, settings: BotSettings) -> Decimal:
    reference = record.last_add_basis_pct
    if reference is None and record.spot_entry_price > 0:
        reference = (record.perp_entry_price - record.spot_entry_price) / record.spot_entry_price * Decimal("100")
    return (reference or Decimal("0")) + settings.add_trigger_spread_pct


def _no(reason: str, trigger_basis: Decimal, current_notional: Decimal, next_notional: Decimal) -> CashCarryAddDecision:
    return CashCarryAddDecision(False, reason, trigger_basis, current_notional, next_notional)
