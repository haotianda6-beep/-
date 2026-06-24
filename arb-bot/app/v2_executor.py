from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.binance_client import BinanceBaseClient, BinanceError
from app.config import Settings
from app.models import ExecutionPlanStatus, OpenPair, OrderRequest, OrderStatus, OrderUpdate, PairDirection, Side, StrategyState, utc_now_ms
from app.mt4_bridge import Mt4Bridge
from app.storage import Storage
from app.strategy import round_down, round_up
from app.v2_support import TERMINAL, execution_status, exit_spread_ready, lots_from_qty, target_exit_spread


class GoldV2Executor:
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
        self.order_created_ms = 0
        self.hedge_started_ms = 0
        self.close_started_ms = 0
        self.last_closed_ms = 0
        self.exit_target_spread: Decimal | None = None

    async def step(self, plan_status: dict[str, Any]) -> None:
        self._process_mt4_reports()
        if self.runtime.state == StrategyState.PAUSED:
            return
        if self.runtime.state == StrategyState.IDLE:
            await self._maybe_place_entry(plan_status)
        elif self.runtime.state == StrategyState.QUOTING_BINANCE_ENTRY:
            await self._check_entry_order(plan_status)
        elif self.runtime.state == StrategyState.HEDGING_MT4:
            self._check_mt4_timeout(self.hedge_started_ms, "MT4 开仓跟随超时")
        elif self.runtime.state == StrategyState.PAIR_OPEN:
            await self._maybe_place_exit(plan_status)
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
        self.order_created_ms = 0
        self.hedge_started_ms = 0
        self.close_started_ms = 0
        self.exit_target_spread = None

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
            self._pause(f"{reason}，但币安已有成交，禁止市价回滚，请人工确认")
            return
        try:
            canceled = await self.binance.cancel_order(self.active_order.order_id)
            self.active_order = canceled or self.active_order
            self.storage.record_event("v2_order_canceled", {"reason": reason, "order_id": self.active_order.order_id})
        except Exception as exc:  # noqa: BLE001
            self.storage.record_event("v2_order_cancel_failed", {"reason": reason, "error": str(exc)[:160]})
        self.active_order = None
        if self.runtime.open_pair is None:
            self.runtime.state = StrategyState.IDLE

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
            self._queue_mt4_hedge(order)
            return
        if order.executed_qty > 0:
            if utc_now_ms() - self.order_created_ms > self.settings.max_hedge_delay_ms:
                self._pause("币安开仓部分成交超时，V2 禁止市价补齐或回滚，请人工确认")
            return
        age = utc_now_ms() - self.order_created_ms
        if age < max(self.settings.min_order_live_ms, self.settings.max_order_age_ms):
            return
        selected = plan_status.get("selected_entry") or {}
        if not selected.get("ready"):
            await self.cancel_active_order("V2 开仓价差回落，撤销未成交限价单")

    async def _maybe_place_exit(self, plan_status: dict[str, Any]) -> None:
        pair = self.runtime.open_pair
        quote = self.binance.latest_quote()
        mt4_quote = self.mt4.latest_quote()
        if not pair or not quote or not mt4_quote:
            return
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
            self._queue_mt4_close()
            return
        if order.executed_qty > 0:
            self._pause("币安平仓部分成交，V2 禁止市价补平，请人工确认")
            return
        pair = self.runtime.open_pair
        if not pair:
            await self.cancel_active_order("V2 组合记录消失")
            return
        target = target_exit_spread(self.settings, pair, plan_status)
        if not exit_spread_ready(pair, self.binance.latest_quote(), self.mt4.latest_quote(), target):
            await self.cancel_active_order("V2 平仓价差回落，撤销未成交限价单")
            return
        if utc_now_ms() - self.order_created_ms > max(self.settings.min_order_live_ms, self.settings.max_order_age_ms):
            await self.cancel_active_order("V2 平仓限价单超时重挂")

    def _queue_mt4_hedge(self, order: OrderUpdate) -> None:
        if not self.entry_hedge_side or not self.entry_direction:
            self._pause("V2 开仓成交缺少方向信息")
            return
        lots = lots_from_qty(self.settings, order.executed_qty)
        command = self.mt4.queue_market_order(self.entry_hedge_side, lots, "v2_entry_follow")
        self.hedge_command_id = command.command_id
        self.hedge_started_ms = utc_now_ms()
        self.runtime.state = StrategyState.HEDGING_MT4
        self.storage.record_event("v2_mt4_entry_queued", {"command_id": command.command_id, "lots": str(lots)})

    def _queue_mt4_close(self) -> None:
        pair = self.runtime.open_pair
        if not pair or not pair.mt4_ticket:
            self._pause("V2 平仓缺少 MT4 单号，币安可能已平，请人工确认")
            return
        command = self.mt4.queue_close(pair.mt4_ticket, lots_from_qty(self.settings, pair.quantity_oz), "v2_exit_follow")
        self.close_command_id = command.command_id
        self.close_started_ms = utc_now_ms()
        self.runtime.state = StrategyState.CLOSING_MT4
        self.storage.record_event("v2_mt4_close_queued", {"command_id": command.command_id, "ticket": pair.mt4_ticket})

    def _process_mt4_reports(self) -> None:
        for report in self.mt4.drain_reports():
            self.storage.record_event("v2_mt4_report", report.model_dump(mode="json"))
            if report.command_id == self.hedge_command_id:
                self._handle_hedge_report(report)
            elif report.command_id == self.close_command_id:
                self._handle_close_report(report)

    def _handle_hedge_report(self, report) -> None:
        if report.status != "ok" or report.fill_price is None or not self.active_order or not self.entry_direction:
            self._pause(f"MT4 开仓跟随失败：{report.message or report.error_code}")
            return
        edge = self.active_order.avg_price - report.fill_price if self.entry_direction == PairDirection.BINANCE_SHORT_MT4_LONG else report.fill_price - self.active_order.avg_price
        self.runtime.open_pair = OpenPair(direction=self.entry_direction, quantity_oz=self.active_order.executed_qty, binance_entry_price=self.active_order.avg_price, mt4_entry_price=report.fill_price, binance_order_id=self.active_order.order_id, mt4_ticket=report.ticket, mt4_tickets=[report.ticket] if report.ticket else [], base_edge=edge)
        self.storage.record_event("v2_pair_open", self.runtime.open_pair.model_dump(mode="json"))
        self.active_order = None
        self.hedge_command_id = None
        self.runtime.state = StrategyState.PAIR_OPEN
        self.runtime.last_error = None

    def _handle_close_report(self, report) -> None:
        if report.status != "ok":
            self._pause(f"MT4 平仓跟随失败：{report.message or report.error_code}")
            return
        pair_id = self.runtime.open_pair.pair_id if self.runtime.open_pair else None
        self.storage.record_event("v2_pair_closed", {"pair_id": pair_id, "ticket": report.ticket})
        self.runtime.open_pair = None
        self.active_order = None
        self.close_command_id = None
        self.last_closed_ms = utc_now_ms()
        self.runtime.state = StrategyState.IDLE
        self.runtime.last_error = None

    async def _refresh_active_order(self) -> OrderUpdate:
        assert self.active_order is not None
        latest = await self.binance.get_order(self.active_order.order_id)
        if latest:
            self.active_order = latest
        return self.active_order

    def _display_exit_spread(self, pair: OpenPair) -> Decimal:
        return self.exit_target_spread if self.exit_target_spread is not None else target_exit_spread(self.settings, pair)

    def _clear_terminal_order(self) -> None:
        if self.active_order and self.active_order.executed_qty > 0:
            self._pause("币安限价单已结束但存在成交数量，请人工确认")
            return
        self.active_order = None
        self.runtime.state = StrategyState.PAIR_OPEN if self.runtime.open_pair else StrategyState.IDLE

    def _check_mt4_timeout(self, started_ms: int, message: str) -> None:
        if started_ms and utc_now_ms() - started_ms > self.settings.max_hedge_delay_ms:
            self._pause(message)

    def _pause(self, reason: str) -> None:
        self.runtime.last_error = reason
        self.runtime.state = StrategyState.PAUSED
        self.storage.record_event("v2_paused", {"reason": reason})

    def _clear_to_idle(self) -> None:
        self.clear()
        self.runtime.state = StrategyState.IDLE
