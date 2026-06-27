import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from app.core.models import BotSettings, CashCarryOpportunity, ExchangeName
from app.services.cash_carry_scope import CASH_CARRY_INTERNAL_CANDIDATE_LIMIT
from app.services.live_market_types import CashCarryScan


SIGNAL_REASON_PREFIXES = ("信号持续不足", "基差波动过大")


@dataclass(frozen=True)
class _SignalSample:
    at: float
    basis_pct: Decimal
    estimated_net_profit: Decimal
    eligible: bool


class CashCarrySignalTracker:
    def __init__(self) -> None:
        self._samples: dict[tuple[ExchangeName, str], deque[_SignalSample]] = {}

    def apply(self, scan: CashCarryScan, settings: BotSettings, now: float | None = None) -> CashCarryScan:
        items = self._unique_items(scan)
        if not items:
            return scan
        timestamp = time.monotonic() if now is None else now
        updated = [self._with_signal_reasons(item, settings, timestamp) for item in items]
        opportunities = [item for item in updated if not item.blocked_reasons]
        candidates = sorted(updated, key=lambda item: (len(item.blocked_reasons), -item.estimated_net_profit))[:CASH_CARRY_INTERNAL_CANDIDATE_LIMIT]
        return CashCarryScan(opportunities=sorted(opportunities, key=lambda item: item.estimated_net_profit, reverse=True), candidates=candidates, issues=scan.issues)

    def _with_signal_reasons(self, item: CashCarryOpportunity, settings: BotSettings, now: float) -> CashCarryOpportunity:
        base_reasons = _without_signal_reasons(item.blocked_reasons)
        key = (ExchangeName(item.exchange), item.symbol)
        eligible = not base_reasons
        samples = self._samples.setdefault(key, deque())
        samples.append(_SignalSample(now, item.basis_pct, item.estimated_net_profit, eligible))
        self._prune(samples, now, settings)
        if not eligible:
            return item.model_copy(update={"blocked_reasons": base_reasons})
        reasons = [*base_reasons, *self._signal_reasons(samples, settings)]
        return item.model_copy(update={"blocked_reasons": reasons})

    def _signal_reasons(self, samples: deque[_SignalSample], settings: BotSettings) -> list[str]:
        ready = self._ready_tail(samples)
        if not ready:
            return ["信号持续不足 0.0s/0样本，等待连续满足开仓条件"]
        duration = Decimal(str(ready[-1].at - ready[0].at))
        min_seconds = settings.cash_carry_signal_min_seconds
        min_samples = settings.cash_carry_signal_min_samples
        if len(ready) < min_samples or duration < min_seconds:
            return [f"信号持续不足 {duration:.1f}s/{len(ready)}样本 < {min_seconds}s/{min_samples}样本"]
        max_swing = settings.cash_carry_signal_max_basis_swing_pct
        if max_swing > 0:
            basis_values = [item.basis_pct for item in ready]
            swing = max(basis_values) - min(basis_values)
            if swing > max_swing:
                return [f"基差波动过大 {swing:.4f}% > {max_swing}%，等待更稳定信号"]
        return []

    def _ready_tail(self, samples: deque[_SignalSample]) -> list[_SignalSample]:
        ready = []
        for item in reversed(samples):
            if not item.eligible:
                break
            ready.append(item)
        return list(reversed(ready))

    def _prune(self, samples: deque[_SignalSample], now: float, settings: BotSettings) -> None:
        keep_seconds = max(float(settings.cash_carry_signal_min_seconds) * 3, 120.0)
        while samples and now - samples[0].at > keep_seconds:
            samples.popleft()

    def _unique_items(self, scan: CashCarryScan) -> list[CashCarryOpportunity]:
        seen: set[tuple[ExchangeName, str]] = set()
        result = []
        for item in [*scan.opportunities, *scan.candidates]:
            key = (ExchangeName(item.exchange), item.symbol)
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result


def _without_signal_reasons(reasons: list[str]) -> list[str]:
    return [reason for reason in reasons if not reason.startswith(SIGNAL_REASON_PREFIXES)]
