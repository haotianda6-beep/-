import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from app.core.models import BotSettings, CashCarryOpportunity, ExchangeName
from app.services.cash_carry_scope import CASH_CARRY_INTERNAL_CANDIDATE_LIMIT
from app.services.live_market_types import CashCarryScan


SIGNAL_REASON_PREFIXES = ("信号持续不足", "基差波动过大", "基差分位样本不足", "基差分位不足")
SIGNAL_ELIGIBLE_PREFIXES = ("V2历史胜率保护", "V3历史胜率保护")
SIGNAL_GAP_GRACE_SECONDS = Decimal("3")


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
        eligibility_reasons = _without_signal_eligible_reasons(base_reasons)
        eligible = not eligibility_reasons
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
        min_seconds, min_samples = self._effective_requirements(ready, settings)
        if len(ready) < min_samples or duration < min_seconds:
            return [f"信号持续不足 {duration:.1f}s/{len(ready)}样本 < {min_seconds}s/{min_samples}样本"]
        max_swing = settings.cash_carry_signal_max_basis_swing_pct
        if max_swing > 0:
            basis_values = [item.basis_pct for item in ready]
            swing = max(basis_values) - min(basis_values)
            if swing > max_swing:
                return [f"基差波动过大 {swing:.4f}% > {max_swing}%，等待更稳定信号"]
        percentile_reason = self._basis_percentile_reason(samples, settings)
        if percentile_reason:
            return [percentile_reason]
        return []

    def _effective_requirements(self, ready: list[_SignalSample], settings: BotSettings) -> tuple[Decimal, int]:
        min_seconds = settings.cash_carry_signal_min_seconds
        min_samples = settings.cash_carry_signal_min_samples
        if not ready or min_seconds <= Decimal("10") or settings.order_notional_usdt <= 0:
            return min_seconds, min_samples
        if not self._has_profit_cushion(ready[-1].estimated_net_profit, settings):
            return min_seconds, min_samples
        return max(Decimal("10"), min_seconds / Decimal("2")), max(min_samples, 5)

    def _basis_percentile_reason(self, samples: deque[_SignalSample], settings: BotSettings) -> str | None:
        min_samples = self._effective_history_samples(samples[-1], settings) if samples else settings.cash_carry_signal_min_history_samples
        if min_samples <= 0 or settings.cash_carry_signal_min_basis_percentile <= 0:
            return None
        if len(samples) < min_samples:
            return f"基差分位样本不足 {len(samples)}/{min_samples}，等待近30分钟分布"
        current_basis = samples[-1].basis_pct
        lower_or_equal = sum(1 for item in samples if item.basis_pct <= current_basis)
        percentile = Decimal(lower_or_equal) / Decimal(len(samples)) * Decimal("100")
        required_percentile = self._effective_basis_percentile(samples[-1], settings)
        if percentile < required_percentile:
            return f"基差分位不足 {percentile:.2f}% < {required_percentile}%，等待相对高位基差"
        return None

    def _effective_history_samples(self, sample: _SignalSample, settings: BotSettings) -> int:
        configured = settings.cash_carry_signal_min_history_samples
        if configured <= 0 or not self._has_profit_cushion(sample.estimated_net_profit, settings):
            return configured
        relaxed = max(20, int((Decimal(configured) * Decimal("0.67")).to_integral_value()))
        return min(configured, relaxed)

    def _effective_basis_percentile(self, sample: _SignalSample, settings: BotSettings) -> Decimal:
        configured = settings.cash_carry_signal_min_basis_percentile
        if configured <= Decimal("70") or not self._has_profit_cushion(sample.estimated_net_profit, settings):
            return configured
        return Decimal("70")

    def _has_profit_cushion(self, estimated_net_profit: Decimal, settings: BotSettings) -> bool:
        if settings.order_notional_usdt <= 0:
            return False
        return estimated_net_profit >= settings.order_notional_usdt * Decimal("0.35") / Decimal("100")

    def _ready_tail(self, samples: deque[_SignalSample]) -> list[_SignalSample]:
        if not samples or not samples[-1].eligible:
            return []
        items = list(samples)
        ready: list[_SignalSample] = []
        gap_budget = SIGNAL_GAP_GRACE_SECONDS
        index = len(items) - 1
        while index >= 0:
            item = items[index]
            if item.eligible:
                ready.append(item)
                index -= 1
                continue
            gap_end = Decimal(str(ready[-1].at if ready else item.at))
            gap_start = Decimal(str(item.at))
            while index >= 0 and not items[index].eligible:
                gap_start = Decimal(str(items[index].at))
                index -= 1
            gap_duration = gap_end - gap_start
            if gap_duration < 0 or gap_duration > gap_budget:
                break
            gap_budget -= gap_duration
        return list(reversed(ready))

    def _prune(self, samples: deque[_SignalSample], now: float, settings: BotSettings) -> None:
        keep_seconds = max(float(settings.cash_carry_signal_min_seconds) * 3, 1800.0)
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


def _without_signal_eligible_reasons(reasons: list[str]) -> list[str]:
    return [reason for reason in reasons if not reason.startswith(SIGNAL_ELIGIBLE_PREFIXES)]
