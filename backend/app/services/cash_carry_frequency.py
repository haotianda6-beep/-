from collections import Counter
from datetime import datetime
from decimal import Decimal

from app.core.models import BotSettings, CashCarryOpportunity, ExchangeName, RiskEvent
from app.services.cash_carry_history_quality import CashCarryHistoryQuality
from app.services.cash_carry_market_memory import CashCarryMarketMemorySummary, CashCarryShadowSummary
from app.services.cash_carry_quality import entry_funding_credit
from app.services.cash_carry_scope import CASH_CARRY_EXCHANGES


BLOCKER_PREFIXES = {
    "基差不足": ("合约溢价未达",),
    "净利不足": ("回归到平仓线后的净利预估", "V3冷启动净利预估", "V3频率调节净利预估", "V2历史胜率保护", "V3历史胜率保护"),
    "资金费不足": ("资金费率不是正数", "资金费率低于"),
    "成交量不足": ("现货/合约最低24h成交量低于",),
    "异常高基差": ("开仓基差异常过高",),
    "信号不稳定": ("信号持续不足", "基差波动过大", "基差分位样本不足", "基差分位不足"),
    "历史亏损币": ("历史发生过强平", "历史累计真实净利", "历史胜率"),
    "标的或链路风险": ("合约与现货标的未确认一致", "预上市合约且现货充提均关闭", "盘口深度不足"),
}

NEAREST_HARD_BLOCKER_PREFIXES = (
    "资金费率不是正数",
    "资金费率低于",
    "现货/合约最低24h成交量低于",
    "开仓基差异常过高",
    "历史发生过强平",
    "历史累计真实净利",
    "历史胜率",
    "合约与现货标的未确认一致",
    "预上市合约且现货充提均关闭",
    "盘口深度不足",
    "最近执行深度失败",
    "同交易所正向期现持仓槽位已满",
    "该交易所该币种已有正向期现持仓",
)


def cash_carry_frequency_event(
    settings: BotSettings,
    candidates: list[CashCarryOpportunity],
    history_quality: CashCarryHistoryQuality,
    now: datetime,
    memory_summary: CashCarryMarketMemorySummary | None = None,
    shadow_summary: CashCarryShadowSummary | None = None,
    shadow_summaries_by_exchange: dict[ExchangeName, CashCarryShadowSummary] | None = None,
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
        required_basis = _required_entry_basis_pct(nearest, settings, gate.min_net_profit, history_quality)
        detail_parts.append(
            f"离开仓最近的是 {nearest.exchange} {nearest.symbol}：预估净利 {nearest.estimated_net_profit:.4f}U，还差 {gap:.4f}U"
        )
        if required_basis is not None:
            detail_parts.append(f"该币按当前本金至少约需基差 {required_basis:.4f}% 才能覆盖安全垫")
    if memory_summary and memory_summary.best:
        best_gap = max(Decimal("0"), gate.min_net_profit - memory_summary.best.estimated_net_profit)
        detail_parts.append(
            f"近{memory_summary.window_minutes}分钟观察 {memory_summary.observations} 次/{memory_summary.symbols} 币，最高为 {memory_summary.best.exchange} {memory_summary.best.symbol}：基差 {memory_summary.best.basis_pct:.4f}%，净利 {memory_summary.best.estimated_net_profit:.4f}U，距门槛 {best_gap:.4f}U"
        )
        detail_parts.append(f"近门槛样本 {memory_summary.near_count} 次，基础质量样本 {memory_summary.base_quality_count} 次")
    if shadow_summary and (shadow_summary.closed_count or shadow_summary.open_count):
        basis_note = (
            f"，赢家最低入场基差 {shadow_summary.min_winning_entry_basis_pct:.4f}%"
            if shadow_summary.min_winning_entry_basis_pct is not None
            else ""
        )
        detail_parts.append(
            f"近{shadow_summary.window_hours}小时探索影子样本：已平 {shadow_summary.closed_count} 单，未平 {shadow_summary.open_count} 单，胜率 {shadow_summary.win_rate_pct:.2f}%，估算净利 {shadow_summary.total_estimated_net:.4f}U，均值 {shadow_summary.avg_estimated_net:.4f}U，最差 {shadow_summary.worst_estimated_net:.4f}U{basis_note}"
        )
    exchange_notes = _exchange_shadow_notes(candidates, shadow_summaries_by_exchange)
    if exchange_notes:
        detail_parts.append("交易所影子门槛：" + "；".join(exchange_notes))
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
        if not any(reason.startswith(NEAREST_HARD_BLOCKER_PREFIXES) for reason in item.blocked_reasons)
    ]
    return max(filtered, key=lambda item: item.estimated_net_profit, default=None)


def _exchange_shadow_notes(
    candidates: list[CashCarryOpportunity],
    summaries: dict[ExchangeName, CashCarryShadowSummary] | None,
) -> list[str]:
    if not summaries:
        return []
    notes: list[str] = []
    for exchange in CASH_CARRY_EXCHANGES:
        summary = summaries.get(exchange)
        if not summary or not (summary.closed_count or summary.open_count):
            continue
        nearest = _nearest_exchange_candidate(candidates, exchange)
        basis_note = ""
        if nearest and summary.min_winning_entry_basis_pct is not None:
            gap = max(Decimal("0"), summary.min_winning_entry_basis_pct - nearest.basis_pct)
            basis_note = f"，当前最近 {nearest.symbol} 基差 {nearest.basis_pct:.4f}%，距影子赢家最低基差还差 {gap:.4f}%"
        elif nearest:
            basis_note = f"，当前最近 {nearest.symbol} 基差 {nearest.basis_pct:.4f}%"
        notes.append(
            f"{exchange.value} 已平 {summary.closed_count} 单，胜率 {summary.win_rate_pct:.2f}%，均值 {summary.avg_estimated_net:.4f}U，最差 {summary.worst_estimated_net:.4f}U{basis_note}"
        )
    return notes


def _nearest_exchange_candidate(candidates: list[CashCarryOpportunity], exchange: ExchangeName) -> CashCarryOpportunity | None:
    pool = [
        item for item in candidates
        if ExchangeName(item.exchange) == exchange
        and not any(reason.startswith(NEAREST_HARD_BLOCKER_PREFIXES) for reason in item.blocked_reasons)
    ]
    return max(pool, key=lambda item: item.estimated_net_profit, default=None)


def _required_entry_basis_pct(
    item: CashCarryOpportunity,
    settings: BotSettings,
    min_net_profit: Decimal,
    history_quality: CashCarryHistoryQuality | None = None,
) -> Decimal | None:
    notional = item.notional_usdt or settings.order_notional_usdt
    if notional <= 0:
        return None
    funding_credit = entry_funding_credit(notional, item.estimated_funding_income)
    required_tradable_pct = (min_net_profit - funding_credit + item.estimated_open_close_fee) / notional * Decimal("100")
    min_basis = settings.cash_carry_min_basis_pct
    if history_quality and history_quality.bootstrap_active(settings):
        min_basis = settings.cash_carry_bootstrap_min_basis_pct
    return max(min_basis, settings.cash_carry_close_basis_pct + required_tradable_pct)
