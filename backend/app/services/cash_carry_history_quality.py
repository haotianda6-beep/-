import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.core.models import BotSettings, ExchangeName


TARGET_WIN_RATE_PCT = Decimal("70")
MIN_TRADES_FOR_WIN_RATE = 2


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
        if stats.trades >= MIN_TRADES_FOR_WIN_RATE and stats.win_rate_pct < TARGET_WIN_RATE_PCT:
            reasons.append(f"历史胜率 {stats.win_rate_pct:.2f}% < {TARGET_WIN_RATE_PCT}%，禁止自动开仓")
        return reasons

    def stats_for(self, exchange: ExchangeName, symbol: str) -> CashCarrySymbolStats:
        self._refresh_if_needed()
        return self._stats.get((ExchangeName(exchange), _normalize_symbol(symbol)), CashCarrySymbolStats())

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
        if not self.state_path.exists():
            return {}
        try:
            positions = json.loads(self.state_path.read_text(encoding="utf-8")).get("positions", [])
        except (OSError, json.JSONDecodeError):
            return {}
        grouped: dict[tuple[ExchangeName, str], list[tuple[Decimal, bool]]] = {}
        for item in positions:
            parsed = self._closed_net(item)
            if parsed is None:
                continue
            try:
                key = (ExchangeName(item["exchange"]), _normalize_symbol(item["symbol"]))
            except (KeyError, ValueError):
                continue
            grouped.setdefault(key, []).append(parsed)
        return {key: self._stats_from(values) for key, values in grouped.items()}

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

    def _decimal(self, value: Any) -> Decimal | None:
        if value in (None, ""):
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None


def _normalize_symbol(symbol: str) -> str:
    return symbol.upper().replace("/", "").replace(":", "").replace("-", "").strip()
