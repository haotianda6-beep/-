from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.core.models import CashCarryOpportunity, ExchangeName


WINDOW = timedelta(minutes=30)


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


class CashCarryMarketMemory:
    def __init__(self) -> None:
        self._samples: deque[CashCarryMarketSample] = deque()

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
        best_pool = base_quality or [item for item in samples if _not_hard_blocked(item)] or samples
        near_floor = dynamic_net_floor * Decimal("0.75")
        near_count = sum(1 for item in base_quality if item.estimated_net_profit >= near_floor)
        return CashCarryMarketMemorySummary(
            observations=len(samples),
            symbols=len({(item.exchange, item.symbol) for item in samples}),
            best=max(best_pool, key=lambda item: item.estimated_net_profit, default=None),
            near_count=near_count,
            base_quality_count=len(base_quality),
        )

    def _prune(self, now: datetime) -> None:
        cutoff = now - WINDOW
        while self._samples and self._samples[0].at < cutoff:
            self._samples.popleft()


def _base_quality_allows(item: CashCarryMarketSample) -> bool:
    return all(reason.startswith(("V2历史胜率保护", "信号持续不足", "基差波动过大")) for reason in item.blocked_reasons)


def _not_hard_blocked(item: CashCarryMarketSample) -> bool:
    return not any(reason.startswith(("开仓基差异常过高", "历史发生过强平", "历史累计真实净利")) for reason in item.blocked_reasons)
