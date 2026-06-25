from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.binance_client import BinanceError
from app.models import OrderRequest, OrderUpdate, PairDirection, Side, StrategyState, utc_now_ms
from app.v2_support import lots_from_qty


class V2AddMixin:
    async def _maybe_place_add(self, plan_status: dict[str, Any]) -> bool:
        pair = self.runtime.open_pair
        plan = plan_status.get("add_plan") or {}
        if not pair or not plan.get("ready"):
            self.add_ready_since_ms = 0
            self._clear_add_confirm_message()
            if not pair:
                self.active_add_base_edge = None
                self.active_add_trigger_edge = None
            return False
        if not self._add_trigger_confirmed(plan):
            return False
        side = Side(plan["binance_side"])
        qty = Decimal(str(plan["quantity_oz"]))
        price = Decimal(str(plan["binance_price"]))
        try:
            order = await self.binance.place_post_only_order(
                OrderRequest(symbol=self.settings.binance_symbol, side=side, quantity=qty, price=price, position_side="SHORT" if side == Side.SELL else "LONG")
            )
        except BinanceError as exc:
            self.runtime.last_error = str(exc)[:240]
            self.storage.record_event("v2_add_order_rejected", {"error": str(exc)[:160], "price": str(price)})
            return True
        self.active_order = order
        self.order_created_ms = utc_now_ms()
        self.entry_direction = pair.direction
        self.entry_hedge_side = Side(plan["mt4_follow_side"])
        self.adding_to_pair = True
        self.active_add_base_edge = Decimal(str(plan["base_edge"]))
        self.active_add_trigger_edge = Decimal(str(plan["next_trigger_edge"]))
        self.add_ready_since_ms = 0
        self.runtime.state = StrategyState.QUOTING_BINANCE_ENTRY
        self.runtime.last_error = None
        self.storage.record_event("v2_add_order", {**order.model_dump(mode="json"), "add_plan": plan})
        return True

    def _add_trigger_confirmed(self, plan: dict[str, Any]) -> bool:
        confirm_ms = self.settings.entry_confirm_ms
        if confirm_ms <= 0:
            return True
        now = utc_now_ms()
        if self.add_ready_since_ms <= 0:
            self.add_ready_since_ms = now
            self.storage.record_event(
                "v2_add_trigger_confirming",
                {
                    "current": str(plan.get("current_edge")),
                    "target": str(plan.get("next_trigger_edge")),
                    "elapsed_ms": 0,
                    "confirm_ms": confirm_ms,
                },
            )
        elapsed = now - self.add_ready_since_ms
        if elapsed < confirm_ms:
            self.runtime.last_error = f"V2 补仓价差已触发，确认中 {elapsed}/{confirm_ms}ms，避免瞬时跳价假触发"
            return False
        self._clear_add_confirm_message()
        return True

    def _clear_add_confirm_message(self) -> None:
        if self.runtime.last_error and self.runtime.last_error.startswith("V2 补仓价差已触发，确认中"):
            self.runtime.last_error = None

    def _active_entry_plan(self, plan_status: dict[str, Any]) -> dict:
        return (plan_status.get("add_plan") if self.adding_to_pair else plan_status.get("selected_entry")) or {}

    def _active_entry_cancel_reason(self) -> str:
        return "V2 补仓价差回落，撤销未成交限价单" if self.adding_to_pair else "V2 开仓价差回落，撤销未成交限价单"

    def _queue_mt4_add_or_entry(self, order: OrderUpdate) -> None:
        if not self.entry_hedge_side or not self.entry_direction:
            self._recover_entry_context_from_order(order)
        if not self.entry_hedge_side or not self.entry_direction:
            self.runtime.last_error = "V2 成交缺少方向信息，等待下一次循环从实盘状态恢复。"
            self.storage.record_event("v2_entry_context_missing_recovering", order.model_dump(mode="json"))
            self.runtime.state = StrategyState.QUOTING_BINANCE_ENTRY
            return
        reason = "v2_add_follow" if self.adding_to_pair else "v2_entry_follow"
        command = self.mt4.queue_market_order(self.entry_hedge_side, lots_from_qty(self.settings, order.executed_qty), reason)
        self.hedge_command_id = command.command_id
        self.hedge_started_ms = utc_now_ms()
        self.runtime.state = StrategyState.HEDGING_MT4
        self.storage.record_event("v2_mt4_add_queued" if self.adding_to_pair else "v2_mt4_entry_queued", {"command_id": command.command_id, "lots": str(command.lots)})

    def _recover_entry_context_from_order(self, order: OrderUpdate) -> None:
        if self.runtime.open_pair:
            self.entry_direction = self.runtime.open_pair.direction
            self.entry_hedge_side = Side.BUY if self.runtime.open_pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG else Side.SELL
            self.storage.record_event(
                "v2_entry_context_recovered_from_pair",
                {"order_id": order.order_id, "direction": self.entry_direction.value, "hedge_side": self.entry_hedge_side.value},
            )
            return
        if order.side == Side.SELL:
            self.entry_direction = PairDirection.BINANCE_SHORT_MT4_LONG
            self.entry_hedge_side = Side.BUY
        elif order.side == Side.BUY:
            self.entry_direction = PairDirection.BINANCE_LONG_MT4_SHORT
            self.entry_hedge_side = Side.SELL
        if self.entry_direction and self.entry_hedge_side:
            self.storage.record_event(
                "v2_entry_context_recovered_from_order",
                {"order_id": order.order_id, "side": order.side.value, "direction": self.entry_direction.value, "hedge_side": self.entry_hedge_side.value},
            )

    def _handle_add_report(self, report) -> bool:
        if not self.adding_to_pair:
            return False
        pair, order = self.runtime.open_pair, self.active_order
        if report.status != "ok" or report.fill_price is None or not pair or not order:
            retry = getattr(self, "_retry_mt4_hedge_after_failure", None)
            if retry:
                retry(f"MT4 补仓跟随失败：{report.message or report.error_code}")
            else:
                self._pause(f"MT4 补仓跟随失败：{report.message or report.error_code}")
            return True
        old_qty, add_qty = pair.quantity_oz, order.executed_qty
        new_qty = old_qty + add_qty
        edge = order.avg_price - report.fill_price if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG else report.fill_price - order.avg_price
        tickets = list(pair.mt4_tickets or ([] if pair.mt4_ticket is None else [pair.mt4_ticket]))
        if report.ticket and report.ticket not in tickets:
            tickets.append(report.ticket)
        self.runtime.open_pair = pair.model_copy(update={
            "quantity_oz": new_qty,
            "binance_entry_price": ((pair.binance_entry_price * old_qty) + (order.avg_price * add_qty)) / new_qty,
            "mt4_entry_price": ((pair.mt4_entry_price * old_qty) + (report.fill_price * add_qty)) / new_qty,
            "binance_order_id": f"{pair.binance_order_id} / {order.order_id}",
            "mt4_tickets": tickets,
            "base_edge": self.active_add_base_edge or pair.base_edge,
            "last_add_edge": edge,
            "last_add_trigger_edge": self.active_add_trigger_edge,
            "add_count": pair.add_count + 1,
        })
        self.storage.record_event("v2_pair_added", self.runtime.open_pair.model_dump(mode="json"))
        self.post_add_exit_block_until_ms = utc_now_ms() + max(5000, self.settings.max_hedge_delay_ms)
        self.active_order = None
        self.hedge_command_id = None
        if hasattr(self, "_clear_entry_carry"):
            self._clear_entry_carry()
        self.adding_to_pair = False
        self.active_add_base_edge = None
        self.active_add_trigger_edge = None
        self.runtime.state = StrategyState.PAIR_OPEN
        self.runtime.last_error = None
        return True
