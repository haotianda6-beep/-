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
        rows = [*self._cash_carry_rows()]
        return sorted(rows, key=lambda row: row.closed_at or row.opened_at, reverse=True)

    def _cash_carry_rows(self) -> list[TradeHistory]:
        rows = []
        for item in self._state_positions("cash_carry_execution_state.json"):
            if not self._cash_history_visible(item):
                continue
            row = self._cash_row(item)
            if row:
                rows.append(row)
        return rows

    def _cash_history_visible(self, item: dict[str, Any]) -> bool:
        if item.get("status") == "closed" and item.get("closed_at"):
            return True
        return item.get("status") == "mismatch" and isinstance(item.get("history"), dict) and item["history"].get("closed_at")

    def _state_positions(self, filename: str) -> list[dict[str, Any]]:
        path = self.root / "config" / filename
        if not path.exists():
            return []
        try:
            positions = json.loads(path.read_text(encoding="utf-8")).get("positions", [])
        except (OSError, json.JSONDecodeError):
            return []
        return positions if isinstance(positions, list) else []

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
                closed_at=self._datetime(history.get("closed_at") or item.get("closed_at")) if history.get("closed_at") or item.get("closed_at") else None,
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
                close_reason=self._close_reason_with_summary(item.get("close_reason"), total_pnl, funding, fee, net, history),
                long_order_ids=self._ids(history.get("long_order_ids"), item.get("spot_order_id"), item.get("close_spot_order_id")),
                short_order_ids=self._ids(history.get("short_order_ids"), item.get("perp_order_id"), item.get("close_perp_order_id")),
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

    def _close_reason_with_summary(
        self,
        reason,
        total_pnl: Decimal,
        funding: Decimal,
        fee: Decimal,
        net: Decimal,
        history: dict[str, Any],
    ) -> str | None:
        base = self._close_reason(reason) or "历史平仓"
        if "原因总结：" in base:
            return base
        enriched_history = {**history, "close_reason": base}
        return f"{base}；原因总结：{self._pnl_summary(total_pnl, funding, fee, net, enriched_history)}"

    def _pnl_summary(
        self,
        total_pnl: Decimal,
        funding: Decimal,
        fee: Decimal,
        net: Decimal,
        history: dict[str, Any],
    ) -> str:
        pieces = []
        estimate = self._decimal(history.get("entry_estimated_net_profit"))
        gap = self._decimal(history.get("actual_vs_entry_estimate"))
        if estimate > 0:
            if gap == 0 and "actual_vs_entry_estimate" not in history:
                gap = net - estimate
            pieces.append(f"开仓预估净利 {estimate} USDT，真实偏差 {gap} USDT")
        if self._is_liquidation(history):
            pieces.append("合约腿发生交易所强平，是主要风险来源")
        if self._has_quantity_mismatch(history):
            pieces.append("现货与合约数量不一致，存在单腿或部分对冲风险")
        if net < 0:
            if total_pnl > 0 and total_pnl + funding > 0 and fee >= total_pnl + funding:
                pieces.append("价差毛利润被真实手续费完全吃掉")
            elif total_pnl > 0 and total_pnl + funding - fee < 0:
                pieces.append("价差毛利润不足以覆盖手续费和资金费")
            elif total_pnl < 0:
                pieces.append("现货和合约合计成交亏损，说明平仓价格未覆盖滑点/价差回撤")
            else:
                pieces.append("资金费和手续费扣减后转为亏损")
        else:
            if funding > 0:
                pieces.append("价差收益叠加资金费收入后盈利")
            elif total_pnl > fee:
                pieces.append("价差毛利润覆盖手续费后盈利")
            else:
                pieces.append("净利为正，但利润空间较薄")
        pieces.append(f"真实净利 {net} USDT")
        return "；".join(pieces)

    def _is_liquidation(self, history: dict[str, Any]) -> bool:
        if str(history.get("external_close_type") or "").lower() == "liquidation":
            return True
        text = json.dumps(history, ensure_ascii=False).lower()
        return "强平" in text or "liquidation" in text or "liq" in text

    def _has_quantity_mismatch(self, history: dict[str, Any]) -> bool:
        status = str(history.get("reconcile_status") or "").lower()
        if status == "mismatch":
            return True
        quantity = history.get("quantity")
        return quantity in (None, "")
