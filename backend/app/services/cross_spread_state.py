import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.core.models import ExchangeName
from app.services.cross_spread_execution_models import CrossSpreadPosition
from app.services.reverse_execution_models import ExecutionResult


class CrossSpreadStateStore:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path

    def load_positions(self) -> list[CrossSpreadPosition]:
        result = []
        for item in self.read().get("positions", []):
            if item.get("status") != "open":
                continue
            position = self._parse_position(item)
            if position:
                result.append(position)
        return result

    def save_position(self, position: CrossSpreadPosition) -> None:
        state = self.read()
        state.setdefault("positions", []).append(self._position_dict(position))
        self.write(state)

    def mark_closed(self, position_id: str, reason: str = "") -> None:
        state = self.read()
        for item in state.get("positions", []):
            if item.get("id") == position_id:
                item["status"] = "closed"
                item["closed_at"] = datetime.now(timezone.utc).isoformat()
                if reason:
                    item["close_reason"] = reason
        self.write(state)

    def remember(self, result: ExecutionResult) -> ExecutionResult:
        state = self.read()
        state["last_result"] = {"id": result.id, "status": result.status, "reason": result.reason, "at": datetime.now(timezone.utc).isoformat()}
        self.write(state)
        return result

    def has_active_records(self) -> bool:
        return bool(self.load_positions())

    def read(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {"positions": []}

    def write(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _parse_position(self, item: dict[str, Any]) -> CrossSpreadPosition | None:
        try:
            return CrossSpreadPosition(
                id=item["id"],
                symbol=item["symbol"],
                long_exchange=ExchangeName(item["long_exchange"]),
                short_exchange=ExchangeName(item["short_exchange"]),
                quantity=Decimal(item["quantity"]),
                long_entry_price=Decimal(item["long_entry_price"]),
                short_entry_price=Decimal(item["short_entry_price"]),
                long_order_id=item.get("long_order_id"),
                short_order_id=item.get("short_order_id"),
                opened_at=datetime.fromisoformat(item["opened_at"]),
                status=item.get("status", "open"),
            )
        except (KeyError, ValueError):
            return None

    def _position_dict(self, item: CrossSpreadPosition) -> dict[str, Any]:
        long_exchange = item.long_exchange.value if hasattr(item.long_exchange, "value") else str(item.long_exchange)
        short_exchange = item.short_exchange.value if hasattr(item.short_exchange, "value") else str(item.short_exchange)
        return {
            **item.__dict__,
            "long_exchange": long_exchange,
            "short_exchange": short_exchange,
            "opened_at": item.opened_at.isoformat(),
            "quantity": str(item.quantity),
            "long_entry_price": str(item.long_entry_price),
            "short_entry_price": str(item.short_entry_price),
        }
