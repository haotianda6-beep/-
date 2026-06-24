from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.binance_client import BinanceBaseClient, BinanceError
from app.config import Settings
from app.models import ExecutionPlanStatus, OpenPair, OrderRequest, OrderStatus, OrderUpdate, PairDirection, Side, StrategyState, utc_now_ms
from app.mt4_bridge import Mt4Bridge
from app.quote_guard import xau_quote_gap_reason
from app.storage import Storage
from app.strategy import round_down, round_up
from app.v2_add import V2AddMixin
from app.v2_common import V2CommonMixin
from app.v2_support import TERMINAL, execution_status, exit_spread_ready, lots_from_qty, target_exit_spread


class GoldV2Executor(V2AddMixin, V2CommonMixin):
    def __init__(self, settings: Settings, binance: BinanceBaseClient, mt4: Mt4Bridge, storage: Storage, runtime: Any) -> None:
        self.settings = settings
        self.binance = binance
        self.mt4 = mt4
        self.storage = storage
        self.runtime = runtime
        self.active_order: OrderUpdate | None = None
        self.entry_direction: PairDirection | None = None
        self.entry_hedge_side: Side | None = None
        self.hedge_command_id: str | None = None
        self.close_command_id: str | None = None
        self.close_command_tickets: dict[str, int] = {}
        self.pending_close_tickets: set[int] = set()
        self.carry_entry_qty = Decimal("0")
        self.carry_entry_notional = Decimal("0")
        self.carry_entry_order_ids: list[str] = []
        self.carry_exit_qty = Decimal("0")
        self.carry_exit_notional = Decimal("0")
        self.carry_exit_order_ids: list[str] = []
        self.order_created_ms = 0
        self.hedge_started_ms = 0
        self.close_started_ms = 0
        self.last_closed_ms = 0
        self.exit_target_spread: Decimal | None = None
        self.adding_to_pair = False
        self.active_add_base_edge: Decimal | None = None
        self.active_add_trigger_edge: Decimal | None = None
        self.post_add_exit_block_until_ms = 0

    async def step(self, plan_status: dict[str, Any]) -> None:
        if self._process_mt4_reports():
            return
        if self.runtime.state == StrategyState.PAUSED:
            if not self._resume_recoverable_paused_state():
                return
        if self.runtime.state == StrategyState.IDLE:
            await self._maybe_place_entry(plan_status)
        elif self.runtime.state == StrategyState.QUOTING_BINANCE_ENTRY:
            await self._check_entry_order(plan_status)
        elif self.runtime.state == StrategyState.HEDGING_MT4:
            self._check_mt4_timeout(self.hedge_started_ms, "MT4 开仓跟随超时")
        elif self.runtime.state == StrategyState.PAIR_OPEN:
            await self._maybe_place_exit(plan_status)
            if self.runtime.state == StrategyState.PAIR_OPEN:
                await self._maybe_place_add(plan_status)
        elif self.runtime.state == StrategyState.QUOTING_BINANCE_EXIT:
            await self._check_exit_order(plan_status)
        elif self.runtime.state == StrategyState.CLOSING_MT4:
            self._check_mt4_timeout(self.close_started_ms, "MT4 平仓跟随超时")

    def clear(self) -> None:
        self.active_order = None
        self.entry_direction = None
        self.entry_hedge_side = None
        self.hedge_command_id = None
        self.close_command_id = None
        self.close_command_tickets = {}
        self.pending_close_tickets = set()
        self.carry_entry_qty = Decimal("0")
        self.carry_entry_notional = Decimal("0")
        self.carry_entry_order_ids = []
        self.carry_exit_qty = Decimal("0")
        self.carry_exit_notional = Decimal("0")
        self.carry_exit_order_ids = []
        self.order_created_ms = 0
        self.hedge_started_ms = 0
        self.close_started_ms = 0
        self.exit_target_spread = None
        self.adding_to_pair = False
        self.active_add_base_edge = None
        self.active_add_trigger_edge = None
        self.post_add_exit_block_until_ms = 0

    async def cancel_active_order(self, reason: str) -> None:
        if not self.active_order:
            return
        if self.active_order.status in TERMINAL:
            self._clear_terminal_order()
            return
        try:
            refreshed = await self.binance.get_order(self.active_order.order_id)
            if refreshed:
                self.active_order = refreshed
        except Exception as exc:  # noqa: BLE001
            self.storage.record_event("v2_cancel_refresh_failed", {"reason": reason, "error": str(exc)[:160]})
        if self.active_order.status in TERMINAL:
            self._clear_terminal_order()
            return
        if self.active_order.executed_qty > 0:
            self.runtime.last_error = f"{reason}，但币安已有部分成交，继续等待完全成交，不停止。"
            self.storage.record_event(
                "v2_cancel_skipped_partial_fill",
                {"reason": reason, **self.active_order.model_dump(mode="json")},
            )
            self.runtime.state = StrategyState.QUOTING_BINANCE_EXIT if self.active_order.reduce_only else StrategyState.QUOTING_BINANCE_ENTRY
            return
        try:
            canceled = await self.binance.cancel_order(self.active_order.order_id)
            self.active_order = canceled or self.active_order
            self.storage.record_event("v2_order_canceled", {"reason": reason, "order_id": self.active_order.order_id})
        except Exception as exc:  # noqa: BLE001
            self.storage.record_event("v2_order_cancel_failed", {"reason": reason, "error": str(exc)[:160]})
        self.active_order = None
        self.runtime.state = StrategyState.PAIR_OPEN if self.runtime.open_pair else StrategyState.IDLE

    def execution_plan_status(self) -> ExecutionPlanStatus:
        return execution_status(
            self.settings, self.active_order, self.runtime.open_pair, self.entry_hedge_side, self._display_exit_spread
        )

    async def _maybe_place_entry(self, plan_status: dict[str, Any]) -> None:
        if self.last_closed_ms and utc_now_ms() - self.last_closed_ms < self.settings.post_exit_reentry_cooldown_ms:
            self.runtime.last_error = "V2 平仓后冷却中，暂不重新开仓"
            return
        plan = plan_status.get("selected_entry") or {}
        if not plan.get("ready"):
            self.runtime.last_error = plan.get("reason")
            return
        side = Side(plan["binance_side"])
        qty = Decimal(str(plan["quantity_oz"]))
        price = Decimal(str(plan["binance_price"]))
        try:
            order = await self.binance.place_post_only_order(
                OrderRequest(symbol=self.settings.binance_symbol, side=side, quantity=qty, price=price, position_side="SHORT" if side == Side.SELL else "LONG")
            )
        except BinanceError as exc:
            self.runtime.last_error = str(exc)[:240]
            self.storage.record_event("v2_entry_order_rejected", {"error": str(exc)[:160], "price": str(price)})
            return
        self.active_order = order
        self.order_created_ms = utc_now_ms()
        self.entry_direction = PairDirection(plan["direction"])
        self.entry_hedge_side = Side(plan["mt4_follow_side"])
        self.runtime.state = StrategyState.QUOTING_BINANCE_ENTRY
        self.runtime.last_error = None
        self.storage.record_event("v2_entry_order", order.model_dump(mode="json"))

    async def _check_entry_order(self, plan_status: dict[str, Any]) -> None:
        if not self.active_order:
            self.runtime.state = StrategyState.IDLE
            return
        order = await self._refresh_active_order()
        if order.status in {OrderStatus.CANCELED, OrderStatus.EXPIRED} and order.executed_qty <= 0:
            self.storage.record_event("v2_entry_order_terminal", order.model_dump(mode="json"))
            self._clear_to_idle()
            return
        if order.status == OrderStatus.REJECTED:
            self.storage.record_event("v2_entry_post_only_rejected", order.model_dump(mode="json"))
            self._clear_to_idle()
            return
        if order.status == OrderStatus.FILLED:
            self._queue_mt4_hedge(self._with_carried_entry_fill(order))
            return
        if order.executed_qty > 0:
            if order.status in TERMINAL:
                await self._replace_remaining_entry_order(order, plan_status)
                return
            self.runtime.last_error = f"币安开仓部分成交 {order.executed_qty}/{order.orig_qty} XAU，继续等待完全成交。"
            return
        age = utc_now_ms() - self.order_created_ms
        if age < max(self.settings.min_order_live_ms, self.settings.max_order_age_ms):
            return
        if not self._active_entry_plan(plan_status).get("ready"):
            await self.cancel_active_order(self._active_entry_cancel_reason())

    async def _maybe_place_exit(self, plan_status: dict[str, Any]) -> None:
        pair = self.runtime.open_pair
        quote = self.binance.latest_quote()
        mt4_quote = self.mt4.latest_quote()
        if not pair or not quote or not mt4_quote:
            return
        gap_reason = xau_quote_gap_reason(quote, mt4_quote)
        if gap_reason:
            self.runtime.last_error = f"报价异常，暂停本轮平仓挂单：{gap_reason}"
            return
        post_add_message = "补仓刚完成，等待币安仓位快照稳定后再允许平仓挂单"
        if utc_now_ms() < self.post_add_exit_block_until_ms:
            self.runtime.last_error = post_add_message
            return
        if self.runtime.last_error == post_add_message:
            self.runtime.last_error = None
        target = target_exit_spread(self.settings, pair, plan_status)
        self.exit_target_spread = target
        if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
            current = quote.ask - mt4_quote.bid
            if current > target:
                return
            price = round_down(min(quote.bid - self.settings.binance_entry_offset_usd, mt4_quote.bid + target), self.binance.filters.tick_size)
            side = Side.BUY
        else:
            current = mt4_quote.ask - quote.bid
            if current > target:
                return
            price = round_up(max(quote.ask + self.settings.binance_entry_offset_usd, mt4_quote.ask - target), self.binance.filters.tick_size)
            side = Side.SELL
        try:
            order = await self.binance.place_post_only_order(
                OrderRequest(symbol=self.settings.binance_symbol, side=side, quantity=pair.quantity_oz, price=price, reduce_only=True, position_side="SHORT" if side == Side.BUY else "LONG")
            )
        except BinanceError as exc:
            self.runtime.last_error = str(exc)[:240]
            self.storage.record_event("v2_exit_order_rejected", {"error": str(exc)[:160], "price": str(price)})
            return
        self.active_order = order
        self.order_created_ms = utc_now_ms()
        self.runtime.state = StrategyState.QUOTING_BINANCE_EXIT
        self.storage.record_event("v2_exit_order", order.model_dump(mode="json"))

    async def _check_exit_order(self, plan_status: dict[str, Any]) -> None:
        if not self.active_order:
            self.runtime.state = StrategyState.PAIR_OPEN if self.runtime.open_pair else StrategyState.IDLE
            return
        order = await self._refresh_active_order()
        if order.status in {OrderStatus.CANCELED, OrderStatus.EXPIRED} and order.executed_qty <= 0:
            self.storage.record_event("v2_exit_order_terminal", order.model_dump(mode="json"))
            self.active_order = None
            self.runtime.state = StrategyState.PAIR_OPEN if self.runtime.open_pair else StrategyState.IDLE
            return
        if order.status == OrderStatus.REJECTED:
            self.storage.record_event("v2_exit_post_only_rejected", order.model_dump(mode="json"))
            self.active_order = None
            self.runtime.state = StrategyState.PAIR_OPEN
            return
        if order.status == OrderStatus.FILLED:
            self._queue_mt4_close(self._with_carried_exit_fill(order))
            return
        if order.executed_qty > 0:
            if order.status in TERMINAL:
                await self._replace_remaining_exit_order(order)
                return
            self.runtime.last_error = f"币安平仓部分成交 {order.executed_qty}/{order.orig_qty} XAU，继续等待完全成交。"
            return
        pair = self.runtime.open_pair
        if not pair:
            await self.cancel_active_order("V2 组合记录消失")
            return
        target = target_exit_spread(self.settings, pair, plan_status)
        binance_quote = self.binance.latest_quote()
        mt4_quote = self.mt4.latest_quote()
        gap_reason = xau_quote_gap_reason(binance_quote, mt4_quote)
        if gap_reason:
            await self.cancel_active_order(f"V2 平仓报价异常，撤销未成交限价单：{gap_reason}")
            return
        if not exit_spread_ready(pair, binance_quote, mt4_quote, target):
            await self.cancel_active_order("V2 平仓价差回落，撤销未成交限价单")
            return
        if utc_now_ms() - self.order_created_ms > max(self.settings.min_order_live_ms, self.settings.max_order_age_ms):
            await self.cancel_active_order("V2 平仓限价单超时重挂")

    def _queue_mt4_hedge(self, order: OrderUpdate) -> None:
        if not self.entry_hedge_side or not self.entry_direction:
            self._pause("V2 开仓成交缺少方向信息")
            return
        self.active_order = order
        self._queue_mt4_add_or_entry(order)

    def _queue_mt4_close(self, order: OrderUpdate | None = None) -> None:
        pair = self.runtime.open_pair
        if not pair:
            self.storage.record_event(
                "v2_binance_risk_close_without_pair",
                {"binance_exit_order": order.model_dump(mode="json") if order else None},
            )
            self.active_order = None
            self.runtime.state = StrategyState.IDLE
            self.runtime.last_error = "币安风险平仓已完成，但缺少组合记录，已等待下一轮实盘对账。"
            return
        tickets = list(pair.mt4_tickets or ([] if pair.mt4_ticket is None else [pair.mt4_ticket]))
        if not tickets:
            self.storage.record_event(
                "v2_exit_missing_mt4_tickets_recovering",
                {"pair_id": pair.pair_id, "binance_exit_order": order.model_dump(mode="json") if order else None},
            )
            self.active_order = None
            self.runtime.state = StrategyState.PAIR_OPEN
            self.runtime.last_error = "V2 平仓缺少 MT4 票号，已回到实盘对账恢复。"
            return
        lots_by_ticket = self._close_lots_by_ticket(tickets, pair.quantity_oz)
        self.close_command_tickets = {}
        self.pending_close_tickets = set()
        for ticket in tickets:
            lots = lots_by_ticket.get(ticket)
            if lots is None or lots <= 0:
                continue
            command = self.mt4.queue_close(ticket, lots, "v2_exit_follow")
            self.close_command_id = command.command_id
            self.close_command_tickets[command.command_id] = ticket
            self.pending_close_tickets.add(ticket)
        if not self.pending_close_tickets:
            self.storage.record_event(
                "v2_exit_no_live_mt4_ticket_recovering",
                {"pair_id": pair.pair_id, "tickets": tickets, "binance_exit_order": order.model_dump(mode="json") if order else None},
            )
            self.active_order = None
            self.runtime.state = StrategyState.PAIR_OPEN
            self.runtime.last_error = "V2 没有可平的 MT4 持仓票，已回到实盘对账恢复。"
            return
        self.close_started_ms = utc_now_ms()
        self.runtime.state = StrategyState.CLOSING_MT4
        self.storage.record_event(
            "v2_mt4_close_queued",
            {
                "binance_exit_order": order.model_dump(mode="json") if order else None,
                "command_ids": list(self.close_command_tickets.keys()),
                "tickets": list(self.pending_close_tickets),
                "lots_by_ticket": {str(ticket): str(lots_by_ticket[ticket]) for ticket in self.pending_close_tickets},
            },
        )

    def _close_lots_by_ticket(self, tickets: list[int], quantity_oz: Decimal) -> dict[int, Decimal]:
        positions = {position.ticket: position.lots for position in self.mt4.positions() if position.ticket in tickets}
        if positions:
            return {ticket: positions[ticket] for ticket in tickets if ticket in positions}
        per_ticket_qty = quantity_oz / Decimal(len(tickets))
        per_ticket_lots = lots_from_qty(self.settings, per_ticket_qty)
        return {ticket: per_ticket_lots for ticket in tickets}

    def _process_mt4_reports(self) -> bool:
        reports = self.mt4.drain_reports()
        for report in reports:
            self.storage.record_event("v2_mt4_report", report.model_dump(mode="json"))
            if report.command_id == self.hedge_command_id:
                self._handle_hedge_report(report)
            elif report.command_id == self.close_command_id or report.command_id in self.close_command_tickets:
                self._handle_close_report(report)
        return bool(reports)

    def _handle_hedge_report(self, report) -> None:
        if self._handle_add_report(report):
            return
        if report.status != "ok" or report.fill_price is None or not self.active_order or not self.entry_direction:
            self._retry_mt4_hedge_after_failure(f"MT4 开仓跟随失败：{report.message or report.error_code}")
            return
        edge = self.active_order.avg_price - report.fill_price if self.entry_direction == PairDirection.BINANCE_SHORT_MT4_LONG else report.fill_price - self.active_order.avg_price
        self.runtime.open_pair = OpenPair(direction=self.entry_direction, quantity_oz=self.active_order.executed_qty, binance_entry_price=self.active_order.avg_price, mt4_entry_price=report.fill_price, binance_order_id=self.active_order.order_id, mt4_ticket=report.ticket, mt4_tickets=[report.ticket] if report.ticket else [], base_edge=edge)
        self.storage.record_event("v2_pair_open", self.runtime.open_pair.model_dump(mode="json"))
        self.active_order = None
        self.hedge_command_id = None
        self._clear_entry_carry()
        self.runtime.state = StrategyState.PAIR_OPEN
        self.runtime.last_error = None

    def _handle_close_report(self, report) -> None:
        if report.status != "ok":
            ticket = report.ticket or self.close_command_tickets.get(report.command_id)
            self.close_command_tickets.pop(report.command_id, None)
            self._retry_mt4_close_ticket(ticket, f"MT4 平仓跟随失败：{report.message or report.error_code}")
            return
        ticket = report.ticket or self.close_command_tickets.get(report.command_id)
        pair_id = self.runtime.open_pair.pair_id if self.runtime.open_pair else None
        if ticket is not None:
            self.pending_close_tickets.discard(ticket)
        self.close_command_tickets.pop(report.command_id, None)
        self.storage.record_event("v2_pair_close_ticket", {"pair_id": pair_id, "ticket": ticket})
        if self.pending_close_tickets:
            return
        tickets = self.runtime.open_pair.mt4_tickets if self.runtime.open_pair else []
        self.storage.record_event("v2_pair_closed", {"pair_id": pair_id, "tickets": tickets})
        self.runtime.open_pair = None
        self.active_order = None
        self.close_command_id = None
        self.close_command_tickets = {}
        self.pending_close_tickets = set()
        self._clear_exit_carry()
        self.last_closed_ms = utc_now_ms()
        self.runtime.state = StrategyState.IDLE
        self.runtime.last_error = None

    async def _refresh_active_order(self) -> OrderUpdate:
        assert self.active_order is not None
        try:
            latest = await self.binance.get_order(self.active_order.order_id)
        except BinanceError as exc:
            if _missing_order_error(exc):
                missing = self.active_order.model_copy(update={"status": OrderStatus.CANCELED})
                self.storage.record_event("v2_active_order_missing", missing.model_dump(mode="json"))
                self.active_order = missing
                return missing
            raise
        if latest:
            self.active_order = latest
        return self.active_order

    def _resume_recoverable_paused_state(self) -> bool:
        if self.active_order:
            self.runtime.state = StrategyState.QUOTING_BINANCE_EXIT if self.active_order.reduce_only else StrategyState.QUOTING_BINANCE_ENTRY
        elif self.pending_close_tickets:
            self.runtime.state = StrategyState.CLOSING_MT4
        elif self.hedge_command_id:
            self.runtime.state = StrategyState.HEDGING_MT4
        elif self.runtime.open_pair:
            self.runtime.state = StrategyState.PAIR_OPEN
        else:
            return False
        self.storage.record_event("v2_paused_auto_resumed", {"reason": self.runtime.last_error, "state": self.runtime.state.value})
        return True

    async def _replace_remaining_entry_order(self, order: OrderUpdate, plan_status: dict[str, Any]) -> None:
        self._carry_entry_fill(order)
        remaining = order.orig_qty - order.executed_qty
        if remaining <= 0:
            self._queue_mt4_hedge(self._carried_entry_order(order))
            return
        plan = self._active_entry_plan(plan_status)
        if not plan.get("ready"):
            self.runtime.last_error = f"币安开仓部分成交后剩余 {remaining} XAU，等待价差恢复后继续补齐。"
            self.active_order = order
            self.runtime.state = StrategyState.QUOTING_BINANCE_ENTRY
            return
        side = Side(plan["binance_side"])
        price = Decimal(str(plan["binance_price"]))
        try:
            new_order = await self.binance.place_post_only_order(
                OrderRequest(
                    symbol=self.settings.binance_symbol,
                    side=side,
                    quantity=remaining,
                    price=price,
                    position_side="SHORT" if side == Side.SELL else "LONG",
                )
            )
        except BinanceError as exc:
            self.runtime.last_error = f"币安开仓部分成交后补齐挂单失败，持续重试：{str(exc)[:160]}"
            self.storage.record_event("v2_entry_remaining_requote_failed", {"error": str(exc)[:160], "remaining": str(remaining)})
            self.active_order = order
            self.runtime.state = StrategyState.QUOTING_BINANCE_ENTRY
            return
        self.active_order = new_order
        self.order_created_ms = utc_now_ms()
        self.runtime.last_error = f"币安开仓部分成交后已重挂剩余 {remaining} XAU。"
        self.storage.record_event("v2_entry_remaining_order", new_order.model_dump(mode="json"))

    async def _replace_remaining_exit_order(self, order: OrderUpdate) -> None:
        pair = self.runtime.open_pair
        if not pair:
            self._recover("币安平仓部分成交后组合记录缺失，等待实盘对账自动清理")
            return
        self._carry_exit_fill(order)
        remaining = pair.quantity_oz - self.carry_exit_qty
        if remaining <= 0:
            self._queue_mt4_close(self._carried_exit_order(order))
            return
        quote = self.binance.latest_quote()
        if not quote:
            self.active_order = order
            self.runtime.last_error = f"币安平仓部分成交后剩余 {remaining} XAU，等待币安报价恢复后继续挂单。"
            self.runtime.state = StrategyState.QUOTING_BINANCE_EXIT
            return
        side = order.side
        if side == Side.BUY:
            price = round_down(quote.bid - self.settings.binance_entry_offset_usd, self.binance.filters.tick_size)
        else:
            price = round_up(quote.ask + self.settings.binance_entry_offset_usd, self.binance.filters.tick_size)
        try:
            new_order = await self.binance.place_post_only_order(
                OrderRequest(
                    symbol=self.settings.binance_symbol,
                    side=side,
                    quantity=remaining,
                    price=price,
                    reduce_only=True,
                    position_side="SHORT" if side == Side.BUY else "LONG",
                )
            )
        except BinanceError as exc:
            self.runtime.last_error = f"币安平仓部分成交后补齐挂单失败，持续重试：{str(exc)[:160]}"
            self.storage.record_event("v2_exit_remaining_requote_failed", {"error": str(exc)[:160], "remaining": str(remaining)})
            self.active_order = order
            self.runtime.state = StrategyState.QUOTING_BINANCE_EXIT
            return
        self.active_order = new_order
        self.order_created_ms = utc_now_ms()
        self.runtime.last_error = f"币安平仓部分成交后已重挂剩余 {remaining} XAU。"
        self.storage.record_event("v2_exit_remaining_order", new_order.model_dump(mode="json"))

    def _carry_entry_fill(self, order: OrderUpdate) -> None:
        if order.order_id in self.carry_entry_order_ids or order.executed_qty <= 0:
            return
        self.carry_entry_qty += order.executed_qty
        self.carry_entry_notional += order.avg_price * order.executed_qty
        self.carry_entry_order_ids.append(order.order_id)

    def _carry_exit_fill(self, order: OrderUpdate) -> None:
        if order.order_id in self.carry_exit_order_ids or order.executed_qty <= 0:
            return
        self.carry_exit_qty += order.executed_qty
        self.carry_exit_notional += order.avg_price * order.executed_qty
        self.carry_exit_order_ids.append(order.order_id)

    def _with_carried_entry_fill(self, order: OrderUpdate) -> OrderUpdate:
        if self.carry_entry_qty <= 0:
            return order
        if order.order_id not in self.carry_entry_order_ids:
            qty = self.carry_entry_qty + order.executed_qty
            notional = self.carry_entry_notional + (order.avg_price * order.executed_qty)
            order_ids = [*self.carry_entry_order_ids, order.order_id]
        else:
            qty = self.carry_entry_qty
            notional = self.carry_entry_notional
            order_ids = list(self.carry_entry_order_ids)
        return order.model_copy(update={"order_id": " / ".join(order_ids), "executed_qty": qty, "avg_price": notional / qty})

    def _with_carried_exit_fill(self, order: OrderUpdate) -> OrderUpdate:
        if self.carry_exit_qty <= 0:
            return order
        if order.order_id not in self.carry_exit_order_ids:
            qty = self.carry_exit_qty + order.executed_qty
            notional = self.carry_exit_notional + (order.avg_price * order.executed_qty)
            order_ids = [*self.carry_exit_order_ids, order.order_id]
        else:
            qty = self.carry_exit_qty
            notional = self.carry_exit_notional
            order_ids = list(self.carry_exit_order_ids)
        return order.model_copy(update={"order_id": " / ".join(order_ids), "executed_qty": qty, "avg_price": notional / qty})

    def _carried_entry_order(self, order: OrderUpdate) -> OrderUpdate:
        return order.model_copy(
            update={
                "order_id": " / ".join(self.carry_entry_order_ids),
                "executed_qty": self.carry_entry_qty,
                "avg_price": self.carry_entry_notional / self.carry_entry_qty,
            }
        )

    def _carried_exit_order(self, order: OrderUpdate) -> OrderUpdate:
        return order.model_copy(
            update={
                "order_id": " / ".join(self.carry_exit_order_ids),
                "executed_qty": self.carry_exit_qty,
                "avg_price": self.carry_exit_notional / self.carry_exit_qty,
            }
        )

    def _clear_entry_carry(self) -> None:
        self.carry_entry_qty = Decimal("0")
        self.carry_entry_notional = Decimal("0")
        self.carry_entry_order_ids = []

    def _clear_exit_carry(self) -> None:
        self.carry_exit_qty = Decimal("0")
        self.carry_exit_notional = Decimal("0")
        self.carry_exit_order_ids = []

    def _handle_mt4_timeout(self, message: str) -> None:
        if self.runtime.state == StrategyState.HEDGING_MT4:
            self._recover_or_retry_mt4_hedge(message)
            return
        if self.runtime.state == StrategyState.CLOSING_MT4:
            self._recover_or_retry_mt4_close(message)
            return
        self._recover(message)

    def _recover_or_retry_mt4_hedge(self, message: str) -> None:
        if not self.active_order:
            self._recover(message)
            return
        if self._recover_hedge_from_positions():
            return
        if self.hedge_command_id and self.mt4.pending_command(self.hedge_command_id):
            self.hedge_started_ms = utc_now_ms()
            self.runtime.state = StrategyState.HEDGING_MT4
            self.runtime.last_error = f"{message}，MT4 开仓命令仍未回报，继续等待，不重复下发。"
            self.storage.record_event("v2_mt4_hedge_pending_wait", {"command_id": self.hedge_command_id, "reason": message})
            return
        self.hedge_started_ms = utc_now_ms()
        self.runtime.last_error = f"{message}，未发现对应 MT4 持仓，已重新发送跟随开仓命令。"
        self.storage.record_event("v2_mt4_hedge_timeout_retry", {"reason": message})
        self._queue_mt4_hedge(self.active_order)

    def _recover_hedge_from_positions(self) -> bool:
        if not self.entry_hedge_side or not self.active_order:
            return False
        existing_tickets = set()
        if self.runtime.open_pair:
            existing_tickets.update(self.runtime.open_pair.mt4_tickets or [])
        candidates = [
            position
            for position in self.mt4.positions()
            if position.symbol == self.settings.mt4_symbol
            and position.side == self.entry_hedge_side
            and position.ticket not in existing_tickets
        ]
        if not candidates:
            return False
        position = max(candidates, key=lambda item: item.ticket)
        report = type(
            "RecoveredMt4Report",
            (),
            {
                "status": "ok",
                "fill_price": position.open_price,
                "ticket": position.ticket,
                "lots": position.lots,
                "message": "recovered from MT4 positions",
                "error_code": 0,
            },
        )()
        self.storage.record_event(
            "v2_mt4_hedge_recovered_from_position",
            {"ticket": position.ticket, "open_price": str(position.open_price), "lots": str(position.lots)},
        )
        self._handle_hedge_report(report)
        return True

    def _recover_or_retry_mt4_close(self, message: str) -> None:
        live_tickets = {position.ticket for position in self.mt4.positions() if position.symbol == self.settings.mt4_symbol}
        self.pending_close_tickets = {ticket for ticket in self.pending_close_tickets if ticket in live_tickets}
        if not self.pending_close_tickets:
            self.storage.record_event("v2_mt4_close_recovered_from_positions", {"reason": message})
            pair_id = self.runtime.open_pair.pair_id if self.runtime.open_pair else None
            tickets = self.runtime.open_pair.mt4_tickets if self.runtime.open_pair else []
            self.storage.record_event("v2_pair_closed", {"pair_id": pair_id, "tickets": tickets, "recovered": True})
            self.runtime.open_pair = None
            self.active_order = None
            self.close_command_id = None
            self.close_command_tickets = {}
            self.pending_close_tickets = set()
            self._clear_exit_carry()
            self.last_closed_ms = utc_now_ms()
            self.runtime.state = StrategyState.IDLE
            self.runtime.last_error = None
            return
        pending_commands = [
            command_id
            for command_id, ticket in self.close_command_tickets.items()
            if ticket in self.pending_close_tickets and self.mt4.pending_command(command_id)
        ]
        if pending_commands:
            self.close_started_ms = utc_now_ms()
            self.runtime.state = StrategyState.CLOSING_MT4
            self.runtime.last_error = f"{message}，MT4 平仓命令仍未回报，继续等待，不重复下发。"
            self.storage.record_event(
                "v2_mt4_close_pending_wait",
                {"command_ids": pending_commands, "tickets": list(self.pending_close_tickets), "reason": message},
            )
            return
        for ticket in list(self.pending_close_tickets):
            self._retry_mt4_close_ticket(ticket, message)

    def _retry_mt4_hedge_after_failure(self, reason: str) -> None:
        if self._recover_hedge_from_positions():
            return
        if self.active_order:
            self.runtime.last_error = f"{reason}，继续重试 MT4 跟随。"
            self.storage.record_event("v2_mt4_hedge_failed_retry", {"reason": reason})
            self._queue_mt4_hedge(self.active_order)
            return
        self._recover(reason)

    def _retry_mt4_close_ticket(self, ticket: int | None, reason: str) -> None:
        if ticket is None:
            self._recover(reason)
            return
        position = next((item for item in self.mt4.positions() if item.ticket == ticket and item.symbol == self.settings.mt4_symbol), None)
        if position is None:
            self.pending_close_tickets.discard(ticket)
            self.storage.record_event("v2_mt4_close_ticket_already_flat", {"ticket": ticket, "reason": reason})
            if not self.pending_close_tickets:
                self._recover_or_retry_mt4_close(reason)
            return
        command = self.mt4.queue_close(ticket, position.lots, "v2_exit_follow_retry")
        self.close_command_id = command.command_id
        self.close_command_tickets[command.command_id] = ticket
        self.pending_close_tickets.add(ticket)
        self.close_started_ms = utc_now_ms()
        self.runtime.state = StrategyState.CLOSING_MT4
        self.runtime.last_error = f"{reason}，已重新发送 MT4 平仓命令。"
        self.storage.record_event(
            "v2_mt4_close_retry_queued",
            {"command_id": command.command_id, "ticket": ticket, "lots": str(position.lots), "reason": reason},
        )


def _missing_order_error(exc: BinanceError) -> bool:
    text = str(exc)
    return '"code":-2013' in text or "Order does not exist" in text
