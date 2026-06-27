import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.core.models import ExchangeName
from app.services.cash_carry_execution_models import CASH_CARRY_RULESET_VERSION, CashCarryPosition
from app.services.execution_models import ExecutionResult


class CashCarryStateStore:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path

    def load_positions(self, include_non_open: bool = False) -> list[CashCarryPosition]:
        result = []
        for item in self.read().get("positions", []):
            status = item.get("status")
            if status == "closed" or (not include_non_open and status != "open"):
                continue
            if include_non_open and status not in {"open", "mismatch"}:
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
        state["last_result"] = {"id": result.id, "status": result.status, "reason": result.reason, "at": datetime.now(timezone.utc).isoformat()}
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
            )
        except (KeyError, ValueError):
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
        }
