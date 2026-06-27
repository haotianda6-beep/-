import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.core.models import BotSettings, ExchangeName
from app.services.cash_carry_execution_models import CASH_CARRY_RULESET_VERSION
from app.services.cash_carry_quality import entry_net_floor


TARGET_WIN_RATE_PCT = Decimal("70")
MIN_TRADES_FOR_WIN_RATE = 2
MIN_TRADES_FOR_GLOBAL_GATE = 10
MIN_ESTIMATE_GAP_SAMPLES = 3


@dataclass(frozen=True)
class CashCarrySymbolStats:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_net: Decimal = Decimal("0")
    forced_closes: int = 0
    max_loss: Decimal = Decimal("0")

    @property
    def win_rate_pct(self) -> Decimal:
        return Decimal("0") if self.trades <= 0 else Decimal(self.wins) / Decimal(self.trades) * Decimal("100")


@dataclass(frozen=True)
class CashCarryPerformanceSummary:
    ruleset_version: str
    total_trades: int
    total_wins: int
    total_net: Decimal
    total_win_rate_pct: Decimal
    trades_24h: int
    wins_24h: int
    net_24h: Decimal
    win_rate_24h_pct: Decimal
    blocked_symbols: int
    ignored_legacy_trades: int
    estimate_sample_count: int
    avg_estimate_gap: Decimal
    estimate_miss_count: int


@dataclass(frozen=True)
class CashCarryEntryQualityGate:
    min_net_profit: Decimal
    base_min_net_profit: Decimal
    reasons: tuple[str, ...] = ()


class CashCarryHistoryQuality:
    def __init__(self, state_path: Path | None = None) -> None:
        root = Path(__file__).resolve().parents[3]
        self.state_path = state_path or root / "config" / "cash_carry_execution_state.json"
        self._cache_key: tuple[int, int] | None = None
        self._stats: dict[tuple[ExchangeName, str], CashCarrySymbolStats] = {}

    def blocked_reasons(self, exchange: ExchangeName, symbol: str, settings: BotSettings) -> list[str]:
        stats = self.stats_for(exchange, symbol)
        if stats.trades <= 0:
            return []
        reasons = []
        loss_limit = max(Decimal("1"), settings.order_notional_usdt * Decimal("0.005"))
        if stats.forced_closes > 0:
            reasons.append("历史发生过强平，禁止自动开仓")
        if stats.total_net <= -loss_limit:
            reasons.append(f"历史累计真实净利 {stats.total_net:.4f}U <= -{loss_limit:.4f}U，禁止自动开仓")
        target_win = settings.cash_carry_target_win_rate_pct or TARGET_WIN_RATE_PCT
        if stats.trades >= MIN_TRADES_FOR_WIN_RATE and stats.win_rate_pct < target_win:
            reasons.append(f"历史胜率 {stats.win_rate_pct:.2f}% < {target_win}%，禁止自动开仓")
        return reasons

    def global_entry_reasons(
        self,
        estimated_net_profit: Decimal,
        settings: BotSettings,
        now: datetime | None = None,
    ) -> list[str]:
        gate = self.entry_quality_gate(settings, now)
        if estimated_net_profit >= gate.min_net_profit or gate.min_net_profit <= gate.base_min_net_profit:
            return []
        reason_text = "；".join(gate.reasons) if gate.reasons else "历史表现未达标"
        return [
            f"V3历史胜率保护：净利预估 {estimated_net_profit:.4f}U < 动态安全垫 {gate.min_net_profit:.4f}U（{reason_text}）"
        ]

    def entry_quality_gate(self, settings: BotSettings, now: datetime | None = None) -> CashCarryEntryQualityGate:
        base = self._base_entry_net_floor(settings)
        if not settings.cash_carry_adaptive_quality_enabled:
            return CashCarryEntryQualityGate(base, base)
        summary = self.performance_summary(settings, now)
        adjusted = base
        reasons: list[str] = []
        target_win = settings.cash_carry_target_win_rate_pct
        if summary.total_trades >= MIN_TRADES_FOR_GLOBAL_GATE and target_win > 0 and summary.total_win_rate_pct < target_win:
            deficit = (target_win - summary.total_win_rate_pct) / target_win
            bump_pct = min(Decimal("1.50"), Decimal("0.50") + deficit * Decimal("2.00"))
            adjusted += settings.order_notional_usdt * bump_pct / Decimal("100")
            reasons.append(f"历史胜率 {summary.total_win_rate_pct:.2f}% < 目标 {target_win:.2f}%")
        if summary.trades_24h > 0 and summary.net_24h < 0:
            adjusted += settings.order_notional_usdt * Decimal("0.30") / Decimal("100")
            reasons.append(f"近24小时净利 {summary.net_24h:.4f}U < 0")
        if summary.total_trades > 0 and summary.total_net < 0:
            avg_loss_pressure = min(
                abs(summary.total_net) / Decimal(summary.total_trades) * Decimal("0.15"),
                settings.order_notional_usdt * Decimal("0.50") / Decimal("100"),
            )
            adjusted += avg_loss_pressure
            reasons.append(f"历史累计净利 {summary.total_net:.4f}U < 0")
        if summary.estimate_sample_count >= MIN_ESTIMATE_GAP_SAMPLES and summary.avg_estimate_gap < 0:
            estimate_pressure = min(
                abs(summary.avg_estimate_gap) * Decimal("1.25"),
                settings.order_notional_usdt * Decimal("0.50") / Decimal("100"),
            )
            adjusted += estimate_pressure
            reasons.append(f"V3真实成交比预估平均低 {abs(summary.avg_estimate_gap):.4f}U")
        capped = min(adjusted, self._max_reasonable_entry_floor(settings))
        if capped < adjusted:
            reasons.append("已按最大开仓基差限制封顶动态安全垫")
        return CashCarryEntryQualityGate(capped, base, tuple(reasons))

    def stats_for(self, exchange: ExchangeName, symbol: str) -> CashCarrySymbolStats:
        self._refresh_if_needed()
        return self._stats.get((ExchangeName(exchange), _normalize_symbol(symbol)), CashCarrySymbolStats())

    def performance_summary(self, settings: BotSettings, now: datetime | None = None) -> CashCarryPerformanceSummary:
        current = now or datetime.now(timezone.utc)
        all_rows = self._closed_rows()
        rows = [row for row in all_rows if row[3] == CASH_CARRY_RULESET_VERSION]
        total = self._summary_values(rows)
        day_rows = [row for row in rows if row[1] and current - row[1] <= timedelta(hours=24)]
        day = self._summary_values(day_rows)
        gaps = [row[4] for row in rows if row[4] is not None]
        blocked = sum(1 for key in self._all_stat_keys() if self.blocked_reasons(key[0], key[1], settings))
        return CashCarryPerformanceSummary(
            ruleset_version=CASH_CARRY_RULESET_VERSION,
            total_trades=total[0],
            total_wins=total[1],
            total_net=total[2],
            total_win_rate_pct=total[3],
            trades_24h=day[0],
            wins_24h=day[1],
            net_24h=day[2],
            win_rate_24h_pct=day[3],
            blocked_symbols=blocked,
            ignored_legacy_trades=len(all_rows) - len(rows),
            estimate_sample_count=len(gaps),
            avg_estimate_gap=sum(gaps, Decimal("0")) / Decimal(len(gaps)) if gaps else Decimal("0"),
            estimate_miss_count=sum(1 for gap in gaps if gap < 0),
        )

    def _refresh_if_needed(self) -> None:
        key = self._file_key()
        if key == self._cache_key:
            return
        self._cache_key = key
        self._stats = self._load_stats()

    def _file_key(self) -> tuple[int, int]:
        if not self.state_path.exists():
            return (0, 0)
        stat = self.state_path.stat()
        return (stat.st_mtime_ns, stat.st_size)

    def _load_stats(self) -> dict[tuple[ExchangeName, str], CashCarrySymbolStats]:
        grouped: dict[tuple[ExchangeName, str], list[tuple[Decimal, bool]]] = {}
        for item, net, _closed_at, forced in self._closed_rows(raw=True):
            try:
                key = (ExchangeName(item["exchange"]), _normalize_symbol(item["symbol"]))
            except (KeyError, ValueError):
                continue
            grouped.setdefault(key, []).append((net, forced))
        return {key: self._stats_from(values) for key, values in grouped.items()}

    def _closed_rows(self, raw: bool = False):
        if not self.state_path.exists():
            return []
        try:
            positions = json.loads(self.state_path.read_text(encoding="utf-8")).get("positions", [])
        except (OSError, json.JSONDecodeError):
            return []
        rows = []
        for item in positions:
            parsed = self._closed_net(item)
            if parsed is None:
                continue
            net, forced = parsed
            closed_at = self._closed_at(item)
            version = str(item.get("strategy_version") or "legacy")
            gap = self._estimate_gap(item)
            rows.append((item, net, closed_at, forced) if raw else (net, closed_at, forced, version, gap))
        return rows

    def _closed_net(self, item: dict[str, Any]) -> tuple[Decimal, bool] | None:
        if item.get("status") != "closed":
            return None
        history = item.get("history") if isinstance(item.get("history"), dict) else {}
        net = self._decimal(history.get("actual_net_profit") or item.get("actual_net_profit"))
        if net is None:
            return None
        reason = str(item.get("close_reason") or "")
        forced = history.get("external_close_type") == "liquidation" or "强平" in reason
        return net, forced

    def _stats_from(self, values: list[tuple[Decimal, bool]]) -> CashCarrySymbolStats:
        total = sum((net for net, _ in values), Decimal("0"))
        wins = sum(1 for net, _ in values if net > 0)
        losses = len(values) - wins
        max_loss = min((net for net, _ in values), default=Decimal("0"))
        forced = sum(1 for _, is_forced in values if is_forced)
        return CashCarrySymbolStats(len(values), wins, losses, total, forced, max_loss)

    def _summary_values(self, rows) -> tuple[int, int, Decimal, Decimal]:
        nets = [row[0] for row in rows]
        trades = len(nets)
        wins = sum(1 for net in nets if net > 0)
        total_net = sum(nets, Decimal("0"))
        win_rate = Decimal("0") if trades <= 0 else Decimal(wins) / Decimal(trades) * Decimal("100")
        return trades, wins, total_net, win_rate

    def _all_stat_keys(self) -> set[tuple[ExchangeName, str]]:
        self._refresh_if_needed()
        return set(self._stats)

    def _base_entry_net_floor(self, settings: BotSettings) -> Decimal:
        return entry_net_floor(settings)

    def _max_reasonable_entry_floor(self, settings: BotSettings) -> Decimal:
        tradable_basis_pct = max(Decimal("0"), settings.cash_carry_max_entry_basis_pct - settings.cash_carry_close_basis_pct)
        if tradable_basis_pct <= 0:
            return max(self._base_entry_net_floor(settings), settings.order_notional_usdt * Decimal("2.00") / Decimal("100"))
        theoretical_basis_profit = settings.order_notional_usdt * tradable_basis_pct / Decimal("100")
        return max(self._base_entry_net_floor(settings), theoretical_basis_profit * Decimal("0.75"))

    def _closed_at(self, item: dict[str, Any]) -> datetime | None:
        raw = item.get("closed_at") or (item.get("history") if isinstance(item.get("history"), dict) else {}).get("closed_at")
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def _estimate_gap(self, item: dict[str, Any]) -> Decimal | None:
        history = item.get("history") if isinstance(item.get("history"), dict) else {}
        gap = self._decimal(history.get("actual_vs_entry_estimate"))
        if gap is not None:
            return gap
        estimated = self._decimal(history.get("entry_estimated_net_profit") or item.get("entry_estimated_net_profit"))
        parsed = self._closed_net(item)
        if estimated is None or parsed is None or estimated <= 0:
            return None
        return parsed[0] - estimated

    def _decimal(self, value: Any) -> Decimal | None:
        if value in (None, ""):
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None


def _normalize_symbol(symbol: str) -> str:
    return symbol.upper().replace("/", "").replace(":", "").replace("-", "").strip()
