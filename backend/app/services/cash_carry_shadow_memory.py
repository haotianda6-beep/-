import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.core.models import CashCarryOpportunity, ExchangeName


SHADOW_WINDOW = timedelta(hours=24)
SHADOW_MAX_HOLD = timedelta(hours=4)
SHADOW_PROBE_MIN_NET_PCT = Decimal("0.05")
SHADOW_CLOSED_LIMIT = 100
SHADOW_OPEN_LIMIT = 50
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
    avg_estimated_net: Decimal = Decimal("0")
    min_winning_entry_basis_pct: Decimal | None = None
    window_hours: int = 24


class CashCarryShadowMemory:
    def __init__(self, state_path: Path | None = None) -> None:
        self.state_path = state_path
        self._shadow_open: dict[tuple[ExchangeName, str], CashCarryShadowPosition] = {}
        self._shadow_closed: deque[CashCarryShadowClosedTrade] = deque()
        self._load()

    def observe(
        self,
        candidates: list[CashCarryOpportunity],
        settings,
        dynamic_net_floor: Decimal,
        now: datetime | None = None,
    ) -> None:
        current = now or datetime.now(timezone.utc)
        latest = {(ExchangeName(item.exchange), item.symbol): item for item in candidates}
        changed = self._close_shadow_positions(latest, settings, current)
        for item in candidates:
            key = (ExchangeName(item.exchange), item.symbol)
            if key in self._shadow_open or not _shadow_entry_allows(item, dynamic_net_floor, settings):
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
            changed = True
        if self._prune_shadow(current):
            changed = True
        if changed:
            self._save()

    def summary(self, now: datetime | None = None) -> CashCarryShadowSummary:
        current = now or datetime.now(timezone.utc)
        if self._prune_shadow(current):
            self._save()
        closed = list(self._shadow_closed)
        wins = sum(1 for item in closed if item.estimated_net_profit > 0)
        total = sum((item.estimated_net_profit for item in closed), Decimal("0"))
        worst = min((item.estimated_net_profit for item in closed), default=Decimal("0"))
        win_rate = Decimal("0") if not closed else Decimal(wins) / Decimal(len(closed)) * Decimal("100")
        avg = Decimal("0") if not closed else total / Decimal(len(closed))
        winning_basis = [item.entry_basis_pct for item in closed if item.estimated_net_profit > 0]
        min_winning_basis = min(winning_basis) if winning_basis else None
        return CashCarryShadowSummary(len(self._shadow_open), len(closed), wins, total, win_rate, worst, avg, min_winning_basis)

    def _close_shadow_positions(
        self,
        latest: dict[tuple[ExchangeName, str], CashCarryOpportunity],
        settings,
        now: datetime,
    ) -> bool:
        changed = False
        for key, position in list(self._shadow_open.items()):
            item = latest.get(key)
            if not item:
                if now - position.opened_at >= SHADOW_MAX_HOLD:
                    self._shadow_closed.append(self._close_shadow_trade(position, position.entry_basis_pct, position.max_basis_pct, now, "样本缺失超时"))
                    self._shadow_open.pop(key, None)
                    changed = True
                continue
            max_basis = max(position.max_basis_pct, item.basis_pct)
            if item.basis_pct <= settings.cash_carry_close_basis_pct:
                self._shadow_closed.append(self._close_shadow_trade(position, item.basis_pct, max_basis, now, "基差回归"))
                self._shadow_open.pop(key, None)
                changed = True
                continue
            if now - position.opened_at >= SHADOW_MAX_HOLD:
                self._shadow_closed.append(self._close_shadow_trade(position, item.basis_pct, max_basis, now, "超时观察"))
                self._shadow_open.pop(key, None)
                changed = True
                continue
            if max_basis != position.max_basis_pct:
                self._shadow_open[key] = CashCarryShadowPosition(**{**position.__dict__, "max_basis_pct": max_basis})
                changed = True
        return changed

    def _close_shadow_trade(self, position: CashCarryShadowPosition, close_basis_pct: Decimal, max_basis_pct: Decimal, now: datetime, reason: str) -> CashCarryShadowClosedTrade:
        net = position.notional_usdt * (position.entry_basis_pct - close_basis_pct) / Decimal("100") - position.open_close_fee
        return CashCarryShadowClosedTrade(position.opened_at, now, position.exchange, position.symbol, position.entry_basis_pct, close_basis_pct, max_basis_pct, net, reason)

    def _prune_shadow(self, now: datetime) -> bool:
        changed = False
        cutoff = now - SHADOW_WINDOW
        while self._shadow_closed and self._shadow_closed[0].closed_at < cutoff:
            self._shadow_closed.popleft()
            changed = True
        for key, position in list(self._shadow_open.items()):
            if position.opened_at < cutoff:
                self._shadow_open.pop(key, None)
                changed = True
        return changed

    def _load(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        payload = _read_state(self.state_path).get("cash_carry_shadow", {})
        self._shadow_open = {key: value for key, value in (_parse_position(item) for item in payload.get("open", [])) if key}
        self._shadow_closed = deque(item for item in (_parse_closed(row) for row in payload.get("closed", [])) if item)

    def _save(self) -> None:
        if not self.state_path:
            return
        state = _read_state(self.state_path)
        state["cash_carry_shadow"] = {
            "open": [_position_dict(item) for item in list(self._shadow_open.values())[-SHADOW_OPEN_LIMIT:]],
            "closed": [_closed_dict(item) for item in list(self._shadow_closed)[-SHADOW_CLOSED_LIMIT:]],
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _shadow_entry_allows(item: CashCarryOpportunity, dynamic_net_floor: Decimal, settings) -> bool:
    if not _shadow_quality_allows(item.blocked_reasons) or any(reason.startswith(HARD_BLOCKER_PREFIXES) or "等待盘口深度确认" in reason for reason in item.blocked_reasons):
        return False
    if item.estimated_net_profit < _shadow_probe_net_floor(dynamic_net_floor, item, settings):
        return False
    notional = item.notional_usdt or settings.order_notional_usdt
    return item.max_safe_notional_usdt is None or item.max_safe_notional_usdt >= notional


def _shadow_quality_allows(reasons: list[str]) -> bool:
    allowed_soft = ("合约溢价未达", "回归到平仓线后的净利预估", "V2历史胜率保护", "V3历史胜率保护", "V3冷启动净利预估", "信号持续不足", "基差波动过大", "基差分位样本不足", "基差分位不足")
    return all(reason.startswith(allowed_soft) for reason in reasons)


def _shadow_probe_net_floor(dynamic_net_floor: Decimal, item: CashCarryOpportunity, settings) -> Decimal:
    notional = item.notional_usdt or settings.order_notional_usdt
    return min(dynamic_net_floor, max(Decimal("0.05"), notional * SHADOW_PROBE_MIN_NET_PCT / Decimal("100")))


def _read_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"positions": []}
    except (OSError, json.JSONDecodeError):
        return {"positions": []}


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_position(item: dict[str, Any]) -> tuple[tuple[ExchangeName, str] | None, CashCarryShadowPosition | None]:
    try:
        opened_at = _parse_dt(item.get("opened_at"))
        values = [_decimal(item.get(key)) for key in ("entry_basis_pct", "entry_estimated_net_profit", "open_close_fee", "notional_usdt", "max_basis_pct")]
        if not opened_at or any(value is None for value in values):
            return None, None
        position = CashCarryShadowPosition(opened_at, ExchangeName(item["exchange"]), str(item["symbol"]), *values)
        return (position.exchange, position.symbol), position
    except (KeyError, ValueError):
        return None, None


def _parse_closed(item: dict[str, Any]) -> CashCarryShadowClosedTrade | None:
    try:
        opened_at = _parse_dt(item.get("opened_at"))
        closed_at = _parse_dt(item.get("closed_at"))
        values = [_decimal(item.get(key)) for key in ("entry_basis_pct", "close_basis_pct", "max_basis_pct", "estimated_net_profit")]
        if not opened_at or not closed_at or any(value is None for value in values):
            return None
        return CashCarryShadowClosedTrade(opened_at, closed_at, ExchangeName(item["exchange"]), str(item["symbol"]), *values, str(item.get("reason") or ""))
    except (KeyError, ValueError):
        return None


def _position_dict(item: CashCarryShadowPosition) -> dict[str, str]:
    return {**_base_dict(item), "entry_estimated_net_profit": str(item.entry_estimated_net_profit), "open_close_fee": str(item.open_close_fee), "notional_usdt": str(item.notional_usdt)}


def _closed_dict(item: CashCarryShadowClosedTrade) -> dict[str, str]:
    return {**_base_dict(item), "closed_at": item.closed_at.isoformat(), "close_basis_pct": str(item.close_basis_pct), "estimated_net_profit": str(item.estimated_net_profit), "reason": item.reason}


def _base_dict(item) -> dict[str, str]:
    return {"opened_at": item.opened_at.isoformat(), "exchange": item.exchange.value, "symbol": item.symbol, "entry_basis_pct": str(item.entry_basis_pct), "max_basis_pct": str(item.max_basis_pct)}
