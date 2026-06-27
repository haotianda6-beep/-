from decimal import Decimal

from app.core.market_math import q
from app.core.models import BotSettings


CLOSE_EXECUTION_BUFFER_PCT = Decimal("0.2")
MAX_QUALITY_SCORE = Decimal("100")
HARD_BLOCKER_PREFIXES = (
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
    "同交易所正向期现持仓槽位已满",
    "该交易所该币种已有正向期现持仓",
)


def entry_net_floor(settings: BotSettings) -> Decimal:
    pct_floor = settings.order_notional_usdt * settings.cash_carry_min_entry_net_pct / Decimal("100")
    return max(settings.min_funding_net_usdt, pct_floor)


def close_execution_buffer(settings: BotSettings) -> Decimal:
    pct_buffer = settings.order_notional_usdt * CLOSE_EXECUTION_BUFFER_PCT / Decimal("100")
    slippage_buffer = settings.order_notional_usdt * settings.max_slippage_pct / Decimal("100")
    return max(Decimal("0.5"), pct_buffer, slippage_buffer)


def cash_carry_quality_score(
    settings: BotSettings,
    basis_pct: Decimal,
    funding_rate: Decimal,
    min_volume_usdt: Decimal,
    estimated_net_profit: Decimal,
    max_safe_notional_usdt: Decimal | None = None,
) -> Decimal:
    score = (
        _net_score(settings, estimated_net_profit)
        + _basis_score(settings, basis_pct)
        + _funding_score(settings, funding_rate)
        + _volume_score(settings, min_volume_usdt)
        + _depth_score(settings, max_safe_notional_usdt)
    )
    return q(min(MAX_QUALITY_SCORE, max(Decimal("0"), score)), "0.01")


def cash_carry_candidate_sort_key(
    settings: BotSettings,
    blocked_reasons: list[str],
    basis_pct: Decimal,
    estimated_net_profit: Decimal,
    quality_score: Decimal,
) -> tuple[int, int, Decimal, Decimal, Decimal, Decimal]:
    hard_blockers = sum(1 for reason in blocked_reasons if reason.startswith(HARD_BLOCKER_PREFIXES))
    net_gap = max(Decimal("0"), entry_net_floor(settings) - estimated_net_profit)
    basis_gap = max(Decimal("0"), settings.cash_carry_min_basis_pct - basis_pct)
    return (hard_blockers, len(blocked_reasons), net_gap, basis_gap, -quality_score, -estimated_net_profit)


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


def entry_basis_risk_reasons(basis_pct: Decimal, settings: BotSettings) -> list[str]:
    if settings.cash_carry_max_entry_basis_pct <= 0:
        return []
    if basis_pct <= settings.cash_carry_max_entry_basis_pct:
        return []
    return [f"开仓基差异常过高 {q(basis_pct)}% > {q(settings.cash_carry_max_entry_basis_pct)}%，不追异常盘"]


def _net_score(settings: BotSettings, estimated_net_profit: Decimal) -> Decimal:
    floor = entry_net_floor(settings)
    if floor <= 0 or estimated_net_profit <= 0:
        return Decimal("0")
    return min(Decimal("40"), estimated_net_profit / floor * Decimal("25"))


def _basis_score(settings: BotSettings, basis_pct: Decimal) -> Decimal:
    if settings.cash_carry_min_basis_pct <= 0 or basis_pct <= settings.cash_carry_min_basis_pct:
        return Decimal("0")
    extra = basis_pct - settings.cash_carry_min_basis_pct
    return min(Decimal("20"), extra / settings.cash_carry_min_basis_pct * Decimal("12"))


def _funding_score(settings: BotSettings, funding_rate: Decimal) -> Decimal:
    funding_pct = funding_rate * Decimal("100")
    if funding_pct <= settings.cash_carry_min_funding_rate_pct:
        return Decimal("0")
    extra = funding_pct - settings.cash_carry_min_funding_rate_pct
    return min(Decimal("15"), extra / Decimal("0.03") * Decimal("15"))


def _volume_score(settings: BotSettings, min_volume_usdt: Decimal) -> Decimal:
    if settings.cash_carry_min_volume_usdt <= 0 or min_volume_usdt <= 0:
        return Decimal("0")
    ratio = min_volume_usdt / settings.cash_carry_min_volume_usdt
    return min(Decimal("15"), ratio * Decimal("5"))


def _depth_score(settings: BotSettings, max_safe_notional_usdt: Decimal | None) -> Decimal:
    if max_safe_notional_usdt is None or settings.order_notional_usdt <= 0:
        return Decimal("3")
    if max_safe_notional_usdt < settings.order_notional_usdt:
        return Decimal("0")
    return min(Decimal("10"), max_safe_notional_usdt / settings.order_notional_usdt * Decimal("5"))
