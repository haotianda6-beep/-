import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.core.models import ExchangeName
from app.services.cash_carry_execution_models import CASH_CARRY_RULESET_VERSION, CashCarryPosition
from app.services.execution_models import ExecutionResult


ACTIVE_POSITION_STATUSES = {"open", "mismatch", "spot_only", "perp_only"}
DEPTH_BLOCK_RETENTION_SECONDS = 3600
DEPTH_BLOCK_RETENTION_LIMIT = 100
DEPTH_BLOCK_REPEAT_COOLDOWN_SECONDS = 900
DEPTH_BLOCK_REPEAT_THRESHOLD = 2
DEPTH_BLOCK_RECHECK_BASIS_DELTA_PCT = Decimal("0.25")


class CashCarryStateStore:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path

    def load_positions(self, include_non_open: bool = False) -> list[CashCarryPosition]:
        result = []
        for item in self.read().get("positions", []):
            status = item.get("status")
            if status == "closed" or (not include_non_open and status != "open"):
                continue
            if include_non_open and status not in ACTIVE_POSITION_STATUSES:
                continue
            position = self._parse_position(item)
            if position:
                result.append(position)
        return result

    def save_position(self, position: CashCarryPosition) -> None:
        state = self.read()
        state.setdefault("positions", []).append(self._position_dict(position))
        self.write(state)

    def mark_closed(self, position_id: str, reason: str = "", extra: dict[str, Any] | None = None) -> None:
        state = self.read()
        for item in state.get("positions", []):
            if item.get("id") == position_id:
                item["status"] = "closed"
                item["closed_at"] = datetime.now(timezone.utc).isoformat()
                if reason:
                    item["close_reason"] = reason
                if extra:
                    item.update(extra)
        self.write(state)

    def mark_status(self, position_id: str, status: str, reason: str = "", extra: dict[str, Any] | None = None) -> None:
        state = self.read()
        for item in state.get("positions", []):
            if item.get("id") == position_id:
                item["status"] = status
                if reason:
                    item["close_reason"] = reason
                elif status == "open":
                    item.pop("close_reason", None)
                if extra:
                    item.update(extra)
        self.write(state)

    def recently_closed_keys(self, cooldown_seconds: int, now: datetime | None = None) -> set[tuple[ExchangeName, str]]:
        current = now or datetime.now(timezone.utc)
        keys = set()
        for item in self.read().get("positions", []):
            closed_at = item.get("closed_at")
            if item.get("status") != "closed" or not closed_at:
                continue
            try:
                closed = datetime.fromisoformat(str(closed_at).replace("Z", "+00:00"))
                if (current - closed).total_seconds() <= cooldown_seconds:
                    keys.add((ExchangeName(item["exchange"]), item["symbol"]))
            except (KeyError, ValueError):
                continue
        return keys

    def recent_depth_blocked_reasons(
        self,
        cooldown_seconds: int,
        now: datetime | None = None,
        repeat_cooldown_seconds: int = DEPTH_BLOCK_REPEAT_COOLDOWN_SECONDS,
        repeat_threshold: int = DEPTH_BLOCK_REPEAT_THRESHOLD,
        repeat_count_window_seconds: int = DEPTH_BLOCK_RETENTION_SECONDS,
        current_basis_by_key: dict[tuple[ExchangeName, str], Decimal] | None = None,
        recheck_basis_delta_pct: Decimal = DEPTH_BLOCK_RECHECK_BASIS_DELTA_PCT,
    ) -> dict[tuple[ExchangeName, str], str]:
        current = now or datetime.now(timezone.utc)
        records = self._parsed_depth_block_records(current)
        repeat_window = max(cooldown_seconds, repeat_cooldown_seconds)
        repeat_counts: dict[tuple[ExchangeName, str], int] = {}
        for key, at, _reason, _basis_pct in records:
            if (current - at).total_seconds() <= repeat_count_window_seconds:
                repeat_counts[key] = repeat_counts.get(key, 0) + 1
        reasons: dict[tuple[ExchangeName, str], str] = {}
        basis_map = current_basis_by_key or {}
        for key, at, reason, failed_basis in records:
            age = (current - at).total_seconds()
            effective_cooldown = repeat_window if repeat_counts.get(key, 0) >= repeat_threshold else cooldown_seconds
            current_basis = basis_map.get(key)
            if age > effective_cooldown and failed_basis is not None and current_basis is not None:
                required = failed_basis + recheck_basis_delta_pct
                if current_basis < required:
                    reasons[key] = (
                        f"最近执行深度失败，需ticker基差高于 {required:.4f}% 后重试；"
                        f"当前 {current_basis:.4f}%：{reason}"
                    )
                    continue
            if age > effective_cooldown:
                continue
            remaining = max(0, int(effective_cooldown - age))
            reasons[key] = f"最近执行深度失败，约 {remaining}s 后重试：{reason}"
        return reasons

    def mark_added(
        self,
        position_id: str,
        quantity: Decimal,
        spot_entry_price: Decimal,
        perp_entry_price: Decimal,
        add_order: dict[str, Any],
    ) -> None:
        state = self.read()
        for item in state.get("positions", []):
            if item.get("id") != position_id:
                continue
            item["quantity"] = str(quantity)
            item["spot_entry_price"] = str(spot_entry_price)
            item["perp_entry_price"] = str(perp_entry_price)
            item["add_count"] = int(item.get("add_count") or 0) + 1
            item["last_add_basis_pct"] = str(add_order["basis_pct"])
            item.setdefault("add_orders", []).append(add_order)
        self.write(state)

    def mark_rebalanced(self, position_id: str, quantity: Decimal, rebalance_order: dict[str, Any]) -> None:
        state = self.read()
        for item in state.get("positions", []):
            if item.get("id") != position_id:
                continue
            item["quantity"] = str(quantity)
            item["status"] = "open"
            item.pop("close_reason", None)
            item.setdefault("rebalance_orders", []).append(rebalance_order)
        self.write(state)

    def remember(self, result: ExecutionResult) -> ExecutionResult:
        state = self.read()
        payload = {"id": result.id, "status": result.status, "reason": result.reason, "at": datetime.now(timezone.utc).isoformat()}
        context = self._result_context(result)
        if context:
            payload.update(context)
        state["last_result"] = payload
        self._remember_depth_block(state, payload)
        self.write(state)
        return result

    def active_keys(self) -> set[tuple[ExchangeName, str]]:
        keys = set()
        for item in self.read().get("positions", []):
            if item.get("status") != "closed":
                try:
                    keys.add((ExchangeName(item["exchange"]), item["symbol"]))
                except (KeyError, ValueError):
                    continue
        return keys

    def active_exchanges(self) -> set[ExchangeName]:
        return {exchange for exchange, _ in self.active_keys()}

    def active_counts_by_exchange(self) -> dict[ExchangeName, int]:
        counts: dict[ExchangeName, int] = {}
        for exchange, _symbol in self.active_keys():
            counts[exchange] = counts.get(exchange, 0) + 1
        return counts

    def read(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {"positions": []}

    def write(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _depth_block_records(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        records = [item for item in state.get("recent_depth_blocks", []) if isinstance(item, dict)]
        last_result = state.get("last_result")
        if isinstance(last_result, dict) and last_result.get("status") == "blocked_by_depth":
            records.append(last_result)
        return records

    def _parsed_depth_block_records(self, current: datetime) -> list[tuple[tuple[ExchangeName, str], datetime, str, Decimal | None]]:
        parsed = []
        seen = set()
        for result in self._depth_block_records(self.read()):
            try:
                at = datetime.fromisoformat(str(result.get("at", "")).replace("Z", "+00:00"))
                if at.tzinfo is None:
                    at = at.replace(tzinfo=timezone.utc)
                if (current - at).total_seconds() > DEPTH_BLOCK_RETENTION_SECONDS:
                    continue
                key = (ExchangeName(result["exchange"]), str(result["symbol"]))
            except (KeyError, ValueError):
                continue
            reason = str(result.get("reason") or "开仓深度不足")
            basis_pct = self._decimal_or_none(result.get("basis_pct"))
            marker = (key, at.isoformat(), reason, str(basis_pct))
            if marker in seen:
                continue
            seen.add(marker)
            parsed.append((key, at, reason, basis_pct))
        return parsed

    def _remember_depth_block(self, state: dict[str, Any], payload: dict[str, Any]) -> None:
        if payload.get("status") != "blocked_by_depth" or not payload.get("exchange") or not payload.get("symbol"):
            return
        current = datetime.now(timezone.utc)
        records = []
        for item in state.get("recent_depth_blocks", []):
            if not isinstance(item, dict):
                continue
            try:
                at = datetime.fromisoformat(str(item.get("at", "")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if (current - at).total_seconds() <= DEPTH_BLOCK_RETENTION_SECONDS:
                records.append(item)
        record = {
            "exchange": payload["exchange"],
            "symbol": payload["symbol"],
            "reason": payload.get("reason") or "开仓深度不足",
            "at": payload.get("at") or current.isoformat(),
        }
        if payload.get("basis_pct") not in (None, ""):
            record["basis_pct"] = payload["basis_pct"]
        if payload.get("estimated_net_profit") not in (None, ""):
            record["estimated_net_profit"] = payload["estimated_net_profit"]
        records.append(record)
        state["recent_depth_blocks"] = records[-DEPTH_BLOCK_RETENTION_LIMIT:]

    def _decimal_or_none(self, value: Any) -> Decimal | None:
        if value in (None, ""):
            return None
        try:
            return Decimal(str(value))
        except (ArithmeticError, ValueError):
            return None

    def _parse_position(self, item: dict[str, Any]) -> CashCarryPosition | None:
        try:
            return CashCarryPosition(
                id=item["id"],
                exchange=ExchangeName(item["exchange"]),
                symbol=item["symbol"],
                base_asset=item["base_asset"],
                quantity=Decimal(item["quantity"]),
                spot_entry_price=Decimal(item["spot_entry_price"]),
                perp_entry_price=Decimal(item["perp_entry_price"]),
                spot_order_id=item.get("spot_order_id"),
                perp_order_id=item.get("perp_order_id"),
                opened_at=datetime.fromisoformat(item["opened_at"]),
                status=item.get("status", "open"),
                add_count=int(item.get("add_count") or 0),
                last_add_basis_pct=Decimal(item["last_add_basis_pct"]) if item.get("last_add_basis_pct") not in (None, "") else None,
                add_orders=item.get("add_orders") if isinstance(item.get("add_orders"), list) else [],
                rebalance_orders=item.get("rebalance_orders") if isinstance(item.get("rebalance_orders"), list) else [],
                strategy_version=str(item.get("strategy_version") or "legacy"),
                entry_basis_pct=Decimal(str(item.get("entry_basis_pct") or "0")),
                entry_estimated_net_profit=Decimal(str(item.get("entry_estimated_net_profit") or "0")),
                entry_estimated_funding_income=Decimal(str(item.get("entry_estimated_funding_income") or "0")),
                entry_estimated_open_close_fee=Decimal(str(item.get("entry_estimated_open_close_fee") or "0")),
                entry_notional_usdt=Decimal(str(item.get("entry_notional_usdt") or "0")),
            )
        except (KeyError, ValueError):
            return None

    def _result_context(self, result: ExecutionResult) -> dict[str, str]:
        item = result.position
        if item is None:
            return {}
        try:
            exchange = item.exchange if hasattr(item, "exchange") else item["exchange"]
            symbol = item.symbol if hasattr(item, "symbol") else item["symbol"]
            exchange_name = ExchangeName(exchange)
        except (KeyError, TypeError, ValueError):
            return {}
        context = {"exchange": exchange_name.value, "symbol": str(symbol)}
        basis = self._field_value(item, "basis_pct")
        if basis not in (None, ""):
            context["basis_pct"] = str(basis)
        net = self._field_value(item, "estimated_net_profit")
        if net not in (None, ""):
            context["estimated_net_profit"] = str(net)
        return context

    def _field_value(self, item: Any, key: str) -> Any:
        if hasattr(item, key):
            return getattr(item, key)
        if isinstance(item, dict):
            return item.get(key)
        return None

    def _position_dict(self, item: CashCarryPosition) -> dict[str, Any]:
        exchange = item.exchange.value if hasattr(item.exchange, "value") else str(item.exchange)
        return {
            **item.__dict__,
            "exchange": exchange,
            "opened_at": item.opened_at.isoformat(),
            "quantity": str(item.quantity),
            "spot_entry_price": str(item.spot_entry_price),
            "perp_entry_price": str(item.perp_entry_price),
            "last_add_basis_pct": str(item.last_add_basis_pct) if item.last_add_basis_pct is not None else None,
            "strategy_version": item.strategy_version or CASH_CARRY_RULESET_VERSION,
            "entry_basis_pct": str(item.entry_basis_pct),
            "entry_estimated_net_profit": str(item.entry_estimated_net_profit),
            "entry_estimated_funding_income": str(item.entry_estimated_funding_income),
            "entry_estimated_open_close_fee": str(item.entry_estimated_open_close_fee),
            "entry_notional_usdt": str(item.entry_notional_usdt),
        }
