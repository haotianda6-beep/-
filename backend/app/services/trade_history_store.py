import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.core.models import ExchangeName, ReconcileStatus, TradeHistory


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class TradeHistoryStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or PROJECT_ROOT

    def load(self) -> list[TradeHistory]:
        rows = [*self._cross_spread_rows(), *self._cash_carry_rows(), *self._reverse_cash_carry_rows()]
        return sorted(rows, key=lambda row: row.closed_at or row.opened_at, reverse=True)

    def _cross_spread_rows(self) -> list[TradeHistory]:
        rows = []
        for item in self._state_positions("cross_spread_execution_state.json"):
            if item.get("status") != "closed" or not item.get("closed_at"):
                continue
            row = self._cross_row(item)
            if row:
                rows.append(row)
        return rows

    def _cash_carry_rows(self) -> list[TradeHistory]:
        rows = []
        for item in self._state_positions("cash_carry_execution_state.json"):
            if item.get("status") != "closed" or not item.get("closed_at"):
                continue
            row = self._cash_row(item)
            if row:
                rows.append(row)
        return rows

    def _reverse_cash_carry_rows(self) -> list[TradeHistory]:
        rows = []
        for item in self._state_positions("reverse_execution_state.json"):
            if item.get("status") != "closed" or not item.get("closed_at"):
                continue
            row = self._reverse_row(item)
            if row:
                rows.append(row)
        return rows

    def _state_positions(self, filename: str) -> list[dict[str, Any]]:
        path = self.root / "config" / filename
        if not path.exists():
            return []
        try:
            positions = json.loads(path.read_text(encoding="utf-8")).get("positions", [])
        except (OSError, json.JSONDecodeError):
            return []
        return positions if isinstance(positions, list) else []

    def _cross_row(self, item: dict[str, Any]) -> TradeHistory | None:
        history = item.get("history") if isinstance(item.get("history"), dict) else {}
        try:
            long_exchange = ExchangeName(item["long_exchange"])
            short_exchange = ExchangeName(item["short_exchange"])
            long_pnl = self._decimal(history.get("long_pnl"))
            short_pnl = self._decimal(history.get("short_pnl"))
            funding = self._decimal(history.get("funding_net"))
            fee = self._decimal(history.get("actual_fee"))
            total = self._decimal(history.get("total_pnl")) or long_pnl + short_pnl
            net = self._decimal(history.get("actual_net_profit")) or total + funding - fee
            return TradeHistory(
                trade_pair_id=item["id"],
                strategy_type="perp_spread",
                symbol=item["symbol"],
                quantity=self._decimal(history.get("quantity") or item["quantity"]),
                opened_at=self._datetime(history.get("opened_at") or item["opened_at"]),
                closed_at=self._datetime(item["closed_at"]),
                long_exchange=long_exchange,
                short_exchange=short_exchange,
                long_open_price=self._decimal(history.get("long_open_price") or item["long_entry_price"]),
                long_close_price=self._optional_decimal(history.get("long_close_price") or item.get("long_close_price")),
                short_open_price=self._decimal(history.get("short_open_price") or item["short_entry_price"]),
                short_close_price=self._optional_decimal(history.get("short_close_price") or item.get("short_close_price")),
                actual_fee=fee,
                total_pnl=total,
                long_pnl=long_pnl,
                short_pnl=short_pnl,
                funding_net=funding,
                actual_net_profit=net,
                close_reason=self._close_reason(item.get("close_reason")),
                long_order_ids=self._ids(history.get("long_order_ids"), item.get("long_order_id"), item.get("close_long_order_id")),
                short_order_ids=self._ids(history.get("short_order_ids"), item.get("short_order_id"), item.get("close_short_order_id")),
                reconcile_status=ReconcileStatus(history.get("reconcile_status") or "pending"),
            )
        except (KeyError, ValueError):
            return None

    def _cash_row(self, item: dict[str, Any]) -> TradeHistory | None:
        history = item.get("history") if isinstance(item.get("history"), dict) else {}
        try:
            exchange = ExchangeName(item["exchange"])
            quantity = self._decimal(history.get("quantity") or item.get("perp_base_quantity") or item["quantity"])
            long_open = self._decimal(history.get("long_open_price") or item["spot_entry_price"])
            short_open = self._decimal(history.get("short_open_price") or item["perp_entry_price"])
            long_close = self._optional_decimal(history.get("long_close_price") or item.get("spot_close_price"))
            short_close = self._optional_decimal(history.get("short_close_price") or item.get("perp_close_price"))
            long_pnl = self._decimal(history.get("long_pnl"))
            short_pnl = self._decimal(history.get("short_pnl"))
            funding = self._decimal(history.get("funding_net"))
            fee = self._decimal(history.get("actual_fee"))
            total_pnl = self._decimal(history.get("total_pnl")) or long_pnl + short_pnl
            net = self._decimal(history.get("actual_net_profit")) or total_pnl + funding - fee
            return TradeHistory(
                trade_pair_id=item["id"],
                strategy_type="cash_carry",
                symbol=item["symbol"],
                quantity=quantity,
                opened_at=self._datetime(history.get("opened_at") or item["opened_at"]),
                closed_at=self._datetime(item["closed_at"]),
                long_exchange=exchange,
                short_exchange=exchange,
                long_open_price=long_open,
                long_close_price=long_close,
                short_open_price=short_open,
                short_close_price=short_close,
                actual_fee=fee,
                total_pnl=total_pnl,
                long_pnl=long_pnl,
                short_pnl=short_pnl,
                funding_net=funding,
                actual_net_profit=net,
                close_reason=self._close_reason(item.get("close_reason")),
                long_order_ids=self._ids(history.get("long_order_ids"), item.get("spot_order_id"), item.get("close_spot_order_id")),
                short_order_ids=self._ids(history.get("short_order_ids"), item.get("perp_order_id"), item.get("close_perp_order_id")),
                reconcile_status=ReconcileStatus(history.get("reconcile_status") or "pending"),
            )
        except (KeyError, ValueError):
            return None

    def _reverse_row(self, item: dict[str, Any]) -> TradeHistory | None:
        history = item.get("history") if isinstance(item.get("history"), dict) else {}
        try:
            exchange = ExchangeName(item["exchange"])
            long_pnl = self._decimal(history.get("spot_pnl") or history.get("long_pnl"))
            short_pnl = self._decimal(history.get("perp_pnl") or history.get("short_pnl"))
            funding = self._decimal(history.get("funding_net"))
            fee = self._decimal(history.get("actual_fee"))
            total = self._decimal(history.get("total_pnl")) or long_pnl + short_pnl
            net = self._decimal(history.get("actual_net_profit")) or total + funding - fee
            return TradeHistory(
                trade_pair_id=item["id"],
                strategy_type="reverse_cash_carry",
                symbol=item["symbol"],
                quantity=self._decimal(history.get("quantity") or item.get("borrowed_quantity") or item["quantity"]),
                opened_at=self._datetime(history.get("opened_at") or item["opened_at"]),
                closed_at=self._datetime(item["closed_at"]),
                long_exchange=exchange,
                short_exchange=exchange,
                long_open_price=self._decimal(history.get("spot_open_price") or history.get("long_open_price") or item["spot_entry_price"]),
                long_close_price=self._optional_decimal(history.get("spot_close_price") or history.get("long_close_price") or item.get("spot_close_price")),
                short_open_price=self._decimal(history.get("perp_open_price") or history.get("short_open_price") or item["perp_entry_price"]),
                short_close_price=self._optional_decimal(history.get("perp_close_price") or history.get("short_close_price") or item.get("perp_close_price")),
                actual_fee=fee,
                total_pnl=total,
                long_pnl=long_pnl,
                short_pnl=short_pnl,
                funding_net=funding,
                actual_net_profit=net,
                close_reason=self._close_reason(item.get("close_reason")),
                long_order_ids=self._ids(history.get("spot_order_ids"), item.get("spot_order_id"), item.get("close_spot_order_id")),
                short_order_ids=self._ids(history.get("perp_order_ids"), item.get("perp_order_id"), item.get("close_perp_order_id")),
                reconcile_status=ReconcileStatus(history.get("reconcile_status") or "pending"),
            )
        except (KeyError, ValueError):
            return None

    def _ids(self, history_ids, *fallbacks) -> list[str]:
        if isinstance(history_ids, list):
            return [str(item) for item in history_ids if item]
        return [str(item) for item in fallbacks if item]

    def _decimal(self, value) -> Decimal:
        return Decimal(str(value or "0"))

    def _optional_decimal(self, value) -> Decimal | None:
        return Decimal(str(value)) if value not in (None, "") else None

    def _datetime(self, value) -> datetime:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    def _close_reason(self, reason) -> str | None:
        if not reason:
            return None
        mapping = {
            "manual_flattened_single_leg_after_perp_closed": "合约腿已平，现货单腿已人工卖出",
            "manual_flattened_spot_only": "仅现货单腿已人工处理",
            "manual_flattened": "人工处理平仓",
        }
        return mapping.get(str(reason), str(reason))
