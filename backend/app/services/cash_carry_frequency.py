from collections import Counter
from datetime import datetime
from decimal import Decimal

from app.core.models import BotSettings, CashCarryOpportunity, RiskEvent
from app.services.cash_carry_history_quality import CashCarryHistoryQuality


BLOCKER_PREFIXES = {
    "基差不足": ("合约溢价未达",),
    "净利不足": ("回归到平仓线后的净利预估", "V2历史胜率保护"),
    "资金费不足": ("资金费率不是正数", "资金费率低于"),
    "成交量不足": ("现货/合约最低24h成交量低于",),
    "异常高基差": ("开仓基差异常过高",),
    "历史亏损币": ("历史发生过强平", "历史累计真实净利", "历史胜率"),
    "标的或链路风险": ("合约与现货标的未确认一致", "预上市合约且现货充提均关闭", "盘口深度不足"),
}


def cash_carry_frequency_event(
    settings: BotSettings,
    candidates: list[CashCarryOpportunity],
    history_quality: CashCarryHistoryQuality,
    now: datetime,
) -> RiskEvent | None:
    if not settings.cash_carry_enabled or not candidates:
        return None
    ready_count = sum(1 for item in candidates if not item.blocked_reasons)
    if ready_count > 0:
        return None
    gate = history_quality.entry_quality_gate(settings, now)
    counts = _blocker_counts(candidates)
    nearest = _nearest_to_entry_gate(candidates)
    detail_parts = [
        f"当前候选 {len(candidates)} 个，可开仓 0 个",
        f"目标约 {settings.cash_carry_target_daily_trades} 单/日",
        f"动态净利安全垫 {gate.min_net_profit:.4f}U",
    ]
    if nearest:
        gap = max(Decimal("0"), gate.min_net_profit - nearest.estimated_net_profit)
        detail_parts.append(
            f"离开仓最近的是 {nearest.exchange} {nearest.symbol}：预估净利 {nearest.estimated_net_profit:.4f}U，还差 {gap:.4f}U"
        )
    if counts:
        detail_parts.append("主要卡点：" + "，".join(f"{name}{count}个" for name, count in counts.most_common(4)))
    return RiskEvent(
        id="cash-carry-frequency-diagnostic",
        severity="info",
        title="正向期现频率诊断",
        detail="；".join(detail_parts) + "。",
        action="当前不建议为了凑单降低净利安全垫；下一步应优先等待更厚基差、提高盘口深度过滤质量，或在真实胜率恢复后由系统自动降低动态门槛。",
        created_at=now,
    )


def _blocker_counts(candidates: list[CashCarryOpportunity]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for item in candidates:
        matched = set()
        for reason in item.blocked_reasons:
            for name, prefixes in BLOCKER_PREFIXES.items():
                if reason.startswith(prefixes):
                    matched.add(name)
        for name in matched:
            counts[name] += 1
    return counts


def _nearest_to_entry_gate(candidates: list[CashCarryOpportunity]) -> CashCarryOpportunity | None:
    filtered = [
        item
        for item in candidates
        if not any(reason.startswith(("开仓基差异常过高", "历史发生过强平", "历史累计真实净利")) for reason in item.blocked_reasons)
    ]
    return max(filtered or candidates, key=lambda item: item.estimated_net_profit, default=None)
