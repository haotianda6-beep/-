from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.core.models import CashCarryOpportunity, ExchangeName


WINDOW = timedelta(minutes=30)
SHADOW_WINDOW = timedelta(hours=24)
SHADOW_MAX_HOLD = timedelta(hours=4)
SHADOW_PROBE_MIN_NET_PCT = Decimal("0.05")
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
    "最近执行深度失败",
    "同交易所正向期现持仓槽位已满",
    "该交易所该币种已有正向期现持仓",
)


@dataclass(frozen=True)
class CashCarryMarketSample:
    at: datetime
    exchange: ExchangeName
    symbol: str
    basis_pct: Decimal
    estimated_net_profit: Decimal
    blocked_reasons: tuple[str, ...]


@dataclass(frozen=True)
class CashCarryMarketMemorySummary:
    observations: int
    symbols: int
    best: CashCarryMarketSample | None
    near_count: int
    base_quality_count: int
    window_minutes: int = 30


@dataclass(frozen=True)
class CashCarryShadowPosition:
    opened_at: datetime
    exchange: ExchangeName
    symbol: str
    entry_basis_pct: Decimal
    entry_estimated_net_profit: Decimal
    open_close_fee: Decimal
    notional_usdt: Decimal
    max_basis_pct: Decimal


@dataclass(frozen=True)
class CashCarryShadowClosedTrade:
    opened_at: datetime
    closed_at: datetime
    exchange: ExchangeName
    symbol: str
    entry_basis_pct: Decimal
    close_basis_pct: Decimal
    max_basis_pct: Decimal
    estimated_net_profit: Decimal
    reason: str


@dataclass(frozen=True)
class CashCarryShadowSummary:
    open_count: int
    closed_count: int
    wins: int
    total_estimated_net: Decimal
    win_rate_pct: Decimal
    worst_estimated_net: Decimal
    window_hours: int = 24


class CashCarryMarketMemory:
    def __init__(self) -> None:
        self._samples: deque[CashCarryMarketSample] = deque()
        self._shadow_open: dict[tuple[ExchangeName, str], CashCarryShadowPosition] = {}
        self._shadow_closed: deque[CashCarryShadowClosedTrade] = deque()

    def observe(self, candidates: list[CashCarryOpportunity], now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        for item in candidates:
            self._samples.append(
                CashCarryMarketSample(
                    at=current,
                    exchange=ExchangeName(item.exchange),
                    symbol=item.symbol,
                    basis_pct=item.basis_pct,
                    estimated_net_profit=item.estimated_net_profit,
                    blocked_reasons=tuple(item.blocked_reasons),
                )
            )
        self._prune(current)

    def summary(self, dynamic_net_floor: Decimal, now: datetime | None = None) -> CashCarryMarketMemorySummary:
        current = now or datetime.now(timezone.utc)
        self._prune(current)
        samples = list(self._samples)
        if not samples:
            return CashCarryMarketMemorySummary(0, 0, None, 0, 0)
        base_quality = [item for item in samples if _base_quality_allows(item)]
        best_pool = base_quality or [item for item in samples if _not_hard_blocked(item)]
        near_floor = dynamic_net_floor * Decimal("0.75")
        near_count = sum(1 for item in base_quality if item.estimated_net_profit >= near_floor)
        return CashCarryMarketMemorySummary(
            observations=len(samples),
            symbols=len({(item.exchange, item.symbol) for item in samples}),
            best=max(best_pool, key=lambda item: item.estimated_net_profit, default=None),
            near_count=near_count,
            base_quality_count=len(base_quality),
        )

    def observe_shadow(
        self,
        candidates: list[CashCarryOpportunity],
        settings,
        dynamic_net_floor: Decimal,
        now: datetime | None = None,
    ) -> None:
        current = now or datetime.now(timezone.utc)
        latest = {(ExchangeName(item.exchange), item.symbol): item for item in candidates}
        self._close_shadow_positions(latest, settings, current)
        for item in candidates:
            key = (ExchangeName(item.exchange), item.symbol)
            if key in self._shadow_open:
                continue
            if not _shadow_entry_allows(item, dynamic_net_floor, settings):
                continue
            notional = item.notional_usdt or settings.order_notional_usdt
            self._shadow_open[key] = CashCarryShadowPosition(
                opened_at=current,
                exchange=key[0],
                symbol=key[1],
                entry_basis_pct=item.basis_pct,
                entry_estimated_net_profit=item.estimated_net_profit,
                open_close_fee=item.estimated_open_close_fee,
                notional_usdt=notional,
                max_basis_pct=item.basis_pct,
            )
        self._prune_shadow(current)

    def shadow_summary(self, now: datetime | None = None) -> CashCarryShadowSummary:
        current = now or datetime.now(timezone.utc)
        self._prune_shadow(current)
        closed = list(self._shadow_closed)
        wins = sum(1 for item in closed if item.estimated_net_profit > 0)
        total = sum((item.estimated_net_profit for item in closed), Decimal("0"))
        worst = min((item.estimated_net_profit for item in closed), default=Decimal("0"))
        win_rate = Decimal("0") if not closed else Decimal(wins) / Decimal(len(closed)) * Decimal("100")
        return CashCarryShadowSummary(
            open_count=len(self._shadow_open),
            closed_count=len(closed),
            wins=wins,
            total_estimated_net=total,
            win_rate_pct=win_rate,
            worst_estimated_net=worst,
        )

    def _prune(self, now: datetime) -> None:
        cutoff = now - WINDOW
        while self._samples and self._samples[0].at < cutoff:
            self._samples.popleft()

    def _close_shadow_positions(
        self,
        latest: dict[tuple[ExchangeName, str], CashCarryOpportunity],
        settings,
        now: datetime,
    ) -> None:
        closed_keys = []
        for key, position in self._shadow_open.items():
            item = latest.get(key)
            if not item:
                continue
            max_basis = max(position.max_basis_pct, item.basis_pct)
            if item.basis_pct <= settings.cash_carry_close_basis_pct:
                self._shadow_closed.append(self._close_shadow_trade(position, item.basis_pct, max_basis, now, "基差回归"))
                closed_keys.append(key)
                continue
            if now - position.opened_at >= SHADOW_MAX_HOLD:
                self._shadow_closed.append(self._close_shadow_trade(position, item.basis_pct, max_basis, now, "超时观察"))
                closed_keys.append(key)
                continue
            self._shadow_open[key] = CashCarryShadowPosition(
                opened_at=position.opened_at,
                exchange=position.exchange,
                symbol=position.symbol,
                entry_basis_pct=position.entry_basis_pct,
                entry_estimated_net_profit=position.entry_estimated_net_profit,
                open_close_fee=position.open_close_fee,
                notional_usdt=position.notional_usdt,
                max_basis_pct=max_basis,
            )
        for key in closed_keys:
            self._shadow_open.pop(key, None)

    def _close_shadow_trade(
        self,
        position: CashCarryShadowPosition,
        close_basis_pct: Decimal,
        max_basis_pct: Decimal,
        now: datetime,
        reason: str,
    ) -> CashCarryShadowClosedTrade:
        net = position.notional_usdt * (position.entry_basis_pct - close_basis_pct) / Decimal("100") - position.open_close_fee
        return CashCarryShadowClosedTrade(
            opened_at=position.opened_at,
            closed_at=now,
            exchange=position.exchange,
            symbol=position.symbol,
            entry_basis_pct=position.entry_basis_pct,
            close_basis_pct=close_basis_pct,
            max_basis_pct=max_basis_pct,
            estimated_net_profit=net,
            reason=reason,
        )

    def _prune_shadow(self, now: datetime) -> None:
        cutoff = now - SHADOW_WINDOW
        while self._shadow_closed and self._shadow_closed[0].closed_at < cutoff:
            self._shadow_closed.popleft()
        stale = [
            key
            for key, position in self._shadow_open.items()
            if position.opened_at < cutoff
        ]
        for key in stale:
            self._shadow_open.pop(key, None)


def _base_quality_allows(item: CashCarryMarketSample) -> bool:
    soft_blockers = (
        "V2历史胜率保护",
        "V3历史胜率保护",
        "V3冷启动净利预估",
        "信号持续不足",
        "基差波动过大",
        "基差分位样本不足",
        "基差分位不足",
    )
    return all(
        reason.startswith(soft_blockers) for reason in item.blocked_reasons
    )


def _not_hard_blocked(item: CashCarryMarketSample) -> bool:
    return not any(reason.startswith(HARD_BLOCKER_PREFIXES) or "等待盘口深度确认" in reason for reason in item.blocked_reasons)


def _shadow_entry_allows(item: CashCarryOpportunity, dynamic_net_floor: Decimal, settings) -> bool:
    sample = CashCarryMarketSample(
        at=datetime.now(timezone.utc),
        exchange=ExchangeName(item.exchange),
        symbol=item.symbol,
        basis_pct=item.basis_pct,
        estimated_net_profit=item.estimated_net_profit,
        blocked_reasons=tuple(item.blocked_reasons),
    )
    if not _shadow_quality_allows(sample) or not _not_hard_blocked(sample):
        return False
    if item.estimated_net_profit < _shadow_probe_net_floor(dynamic_net_floor, item, settings):
        return False
    notional = item.notional_usdt or settings.order_notional_usdt
    if item.max_safe_notional_usdt is not None and item.max_safe_notional_usdt < notional:
        return False
    return True


def _shadow_quality_allows(item: CashCarryMarketSample) -> bool:
    allowed_soft = (
        "合约溢价未达",
        "回归到平仓线后的净利预估",
        "V2历史胜率保护",
        "V3历史胜率保护",
        "V3冷启动净利预估",
        "信号持续不足",
        "基差波动过大",
        "基差分位样本不足",
        "基差分位不足",
    )
    return all(reason.startswith(allowed_soft) for reason in item.blocked_reasons)


def _shadow_probe_net_floor(dynamic_net_floor: Decimal, item: CashCarryOpportunity, settings) -> Decimal:
    notional = item.notional_usdt or settings.order_notional_usdt
    probe_floor = max(Decimal("0.05"), notional * SHADOW_PROBE_MIN_NET_PCT / Decimal("100"))
    return min(dynamic_net_floor, probe_floor)
