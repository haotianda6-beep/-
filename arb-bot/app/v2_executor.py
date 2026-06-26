from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
from typing import Any

from app.binance_client import BinanceBaseClient, BinanceError
from app.config import Settings
from app.gold_v2_version import GOLD_V2_CURRENT_GUARD_START_MS
from app.market_calendar import xau_weekend_entry_block_reason
from app.models import ExecutionPlanStatus, OpenPair, OrderRequest, OrderStatus, OrderUpdate, PairDirection, Side, StrategyState, utc_now_ms
from app.mt4_bridge import Mt4Bridge
from app.quote_guard import xau_quote_gap_reason
from app.storage import Storage
from app.strategy import round_down, round_up
from app.v2_add import V2AddMixin
from app.v2_common import V2CommonMixin
from app.v2_support import TERMINAL, execution_status, exit_spread_ready, lots_from_qty, target_exit_spread


MT4_MARKET_CLOSED_ERROR_CODES = {132}
MT4_EXIT_BLOCK_MS = 30 * 60 * 1000
REQUIRED_MT4_EA_VERSION = "20260626-trade-guard-v2"


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
        self.close_mt4_qty = Decimal("0")
        self.close_mt4_notional = Decimal("0")
        self.close_mt4_quote_bid: Decimal | None = None
        self.close_mt4_quote_ask: Decimal | None = None
        self.close_mt4_quote_ms: int | None = None
        self.close_mt4_report_ms: int | None = None
        self.entry_mt4_quote_bid: Decimal | None = None
        self.entry_mt4_quote_ask: Decimal | None = None
        self.entry_mt4_quote_ms: int | None = None
        self.carry_entry_qty = Decimal("0")
        self.carry_entry_notional = Decimal("0")
        self.carry_entry_order_ids: list[str] = []
        self.carry_exit_qty = Decimal("0")
        self.carry_exit_notional = Decimal("0")
        self.carry_exit_order_ids: list[str] = []
        self.order_created_ms = 0
        self.exit_ready_since_ms = 0
        self.add_ready_since_ms = 0
        self.add_confirm_event_ms = 0
        self.risk_exit_ready_since_ms = 0
        self.risk_exit_confirm_reason: str | None = None
        self.hedge_started_ms = 0
        self.close_started_ms = 0
        self.last_closed_ms = 0
        self.last_entry_ms = self._load_last_entry_ms()
        self.exit_target_spread: Decimal | None = None
        self.adding_to_pair = False
        self.active_add_base_edge: Decimal | None = None
        self.active_add_trigger_edge: Decimal | None = None
        self.active_mt4_follow_min_edge: Decimal | None = None
        self.post_add_exit_block_until_ms = 0
        self.entry_requote_until_ms = 0
        self.repairing_binance_only = False
        self.repair_existing_qty = Decimal("0")
        self.mt4_exit_block_until_ms = 0
        self.mt4_exit_block_seeded_from_last_error = False
        self.active_exit_order_risk_active = False
        self.active_exit_order_risk_reason: str | None = None

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
        self.close_mt4_qty = Decimal("0")
        self.close_mt4_notional = Decimal("0")
        self.close_mt4_quote_bid = None
        self.close_mt4_quote_ask = None
        self.close_mt4_quote_ms = None
        self.close_mt4_report_ms = None
        self.entry_mt4_quote_bid = None
        self.entry_mt4_quote_ask = None
        self.entry_mt4_quote_ms = None
        self.carry_entry_qty = Decimal("0")
        self.carry_entry_notional = Decimal("0")
        self.carry_entry_order_ids = []
        self.carry_exit_qty = Decimal("0")
        self.carry_exit_notional = Decimal("0")
        self.carry_exit_order_ids = []
        self.order_created_ms = 0
        self.exit_ready_since_ms = 0
        self.add_ready_since_ms = 0
        self.add_confirm_event_ms = 0
        self.risk_exit_ready_since_ms = 0
        self.risk_exit_confirm_reason = None
        self.hedge_started_ms = 0
        self.close_started_ms = 0
        self.exit_target_spread = None
        self.adding_to_pair = False
        self.active_add_base_edge = None
        self.active_add_trigger_edge = None
        self.active_mt4_follow_min_edge = None
        self.post_add_exit_block_until_ms = 0
        self.entry_requote_until_ms = 0
        self.repairing_binance_only = False
        self.repair_existing_qty = Decimal("0")
        self.mt4_exit_block_until_ms = 0
        self.mt4_exit_block_seeded_from_last_error = False
        self.active_exit_order_risk_active = False
        self.active_exit_order_risk_reason = None

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
            was_entry_order = not self.active_order.reduce_only
            canceled = await self.binance.cancel_order(self.active_order.order_id)
            self.active_order = canceled or self.active_order
            self.storage.record_event("v2_order_canceled", {"reason": reason, "order_id": self.active_order.order_id})
            if was_entry_order:
                self.entry_requote_until_ms = utc_now_ms() + max(0, self.settings.requote_cooldown_ms)
        except Exception as exc:  # noqa: BLE001
            self.storage.record_event("v2_order_cancel_failed", {"reason": reason, "error": str(exc)[:160]})
        self.active_order = None
        self._clear_active_order_context()
        self.runtime.state = StrategyState.PAIR_OPEN if self.runtime.open_pair else StrategyState.IDLE

    def execution_plan_status(self) -> ExecutionPlanStatus:
        return execution_status(
            self.settings, self.active_order, self.runtime.open_pair, self.entry_hedge_side, self._display_exit_spread
        )

    async def _maybe_place_entry(self, plan_status: dict[str, Any]) -> None:
        mt4_block_reason = self._mt4_trade_block_reason("开仓")
        if mt4_block_reason:
            self.runtime.last_error = mt4_block_reason
            return
        if self._entry_interval_blocked():
            return
        if self.last_closed_ms and utc_now_ms() - self.last_closed_ms < self.settings.post_exit_reentry_cooldown_ms:
            self.runtime.last_error = "V2 平仓后冷却中，暂不重新开仓"
            return
        if self._entry_requote_blocked():
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
        self._clear_active_order_context()
        self.order_created_ms = utc_now_ms()
        self.entry_direction = PairDirection(plan["direction"])
        self.entry_hedge_side = Side(plan["mt4_follow_side"])
        self.active_mt4_follow_min_edge = self._mt4_follow_min_edge_from_plan(plan)
        self.runtime.state = StrategyState.QUOTING_BINANCE_ENTRY
        self.runtime.last_error = None
        self.storage.record_event("v2_entry_order", order.model_dump(mode="json"))

    def _entry_requote_blocked(self) -> bool:
        if self.entry_requote_until_ms <= 0:
            return False
        now = utc_now_ms()
        if now >= self.entry_requote_until_ms:
            self.entry_requote_until_ms = 0
            if self.runtime.last_error and self.runtime.last_error.startswith("V2 开仓撤单冷却中"):
                self.runtime.last_error = None
            return False
        seconds_left = max(1, (self.entry_requote_until_ms - now) // 1000)
        self.runtime.last_error = f"V2 开仓撤单冷却中，约 {seconds_left} 秒后再允许重新挂单"
        return True

    async def _check_entry_order(self, plan_status: dict[str, Any]) -> None:
        if self.repairing_binance_only:
            await self._check_binance_restore_order()
            return
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
            self._schedule_binance_fill_audit(order, "entry")
            self._queue_mt4_hedge(self._with_carried_entry_fill(order))
            return
        if order.executed_qty > 0:
            if order.status in TERMINAL:
                await self._replace_remaining_entry_order(order, plan_status)
                return
            self.runtime.last_error = f"币安开仓部分成交 {order.executed_qty}/{order.orig_qty} XAU，继续等待完全成交。"
            return
        if await self._cancel_entry_order_if_locked_edge_invalid(plan_status):
            return
        age = utc_now_ms() - self.order_created_ms
        if age < max(self.settings.min_order_live_ms, self.settings.max_order_age_ms):
            return
        if not self._active_entry_plan(plan_status).get("ready"):
            await self.cancel_active_order(self._active_entry_cancel_reason())

    async def _cancel_entry_order_if_locked_edge_invalid(self, plan_status: dict[str, Any]) -> bool:
        reason = self._entry_locked_edge_cancel_reason(plan_status)
        if not reason:
            return False
        await self.cancel_active_order(reason)
        return True

    def _entry_locked_edge_cancel_reason(self, plan_status: dict[str, Any]) -> str | None:
        order = self.active_order
        if not order or order.reduce_only or order.executed_qty > 0:
            return None
        plan = self._active_entry_plan(plan_status)
        if not plan or not plan.get("ready"):
            return None
        mt4_quote = self.mt4.latest_quote()
        if not mt4_quote:
            return None
        gap_reason = xau_quote_gap_reason(self.binance.latest_quote(), mt4_quote)
        if gap_reason:
            return f"V2 开仓挂单报价异常，撤销未成交限价单：{gap_reason}"
        required = self._active_entry_required_locked_edge(plan)
        if required is None:
            return None
        locked = self._active_order_locked_edge(order, mt4_quote)
        if locked is None:
            return None
        if locked < required:
            return f"V2 开仓挂单锁定价差 {locked} 低于安全线 {required}，撤销未成交限价单"
        return None

    def _active_entry_required_locked_edge(self, plan: dict[str, Any]) -> Decimal | None:
        direct = self._plan_decimal(plan, "locked_edge_floor")
        if direct is not None:
            return direct
        actionable = self._plan_decimal(plan, "next_actionable_trigger_edge")
        if actionable is not None:
            return actionable + (self._plan_decimal(plan, "mt4_slippage_budget") or Decimal("0"))
        required = self._plan_decimal(plan, "required_edge") or self._plan_decimal(plan, "threshold")
        if required is None:
            return None
        return required + (self._plan_decimal(plan, "mt4_slippage_budget") or Decimal("0"))

    def _mt4_follow_min_edge_from_plan(self, plan: dict[str, Any]) -> Decimal | None:
        candidates = [
            value
            for value in (
                self._plan_decimal(plan, "locked_edge_floor"),
                self._plan_decimal(plan, "next_locked_trigger_edge"),
                self._plan_decimal(plan, "required_locked_edge"),
                self._plan_decimal(plan, "required_edge"),
                self._plan_decimal(plan, "threshold"),
            )
            if value is not None
        ]
        actionable = self._plan_decimal(plan, "next_actionable_trigger_edge")
        if actionable is not None:
            candidates.append(actionable + max(Decimal("0"), self._plan_decimal(plan, "mt4_slippage_budget") or Decimal("0")))
        return max(candidates) if candidates else None

    def _mt4_follow_price_limits(self, order: OrderUpdate) -> tuple[Decimal | None, Decimal | None]:
        min_edge = self.active_mt4_follow_min_edge
        if min_edge is None or min_edge <= 0 or not self.entry_hedge_side:
            return None, None
        if self.entry_hedge_side == Side.BUY:
            return order.avg_price - min_edge, None
        if self.entry_hedge_side == Side.SELL:
            return None, order.avg_price + min_edge
        return None, None

    def _active_order_locked_edge(self, order: OrderUpdate, mt4_quote) -> Decimal | None:
        if order.side == Side.SELL:
            return order.price - mt4_quote.ask
        if order.side == Side.BUY:
            return mt4_quote.bid - order.price
        return None

    def _plan_decimal(self, plan: dict[str, Any], key: str) -> Decimal | None:
        value = plan.get(key)
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None

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
        mt4_block_reason = self._mt4_trade_block_reason("平仓")
        if mt4_block_reason:
            self.exit_ready_since_ms = 0
            self.runtime.last_error = mt4_block_reason
            return
        post_add_message = "开仓或补仓刚完成，等待仓位和 MT4 报价稳定后再允许平仓挂单"
        post_add_messages = (post_add_message, "补仓刚完成，等待币安仓位快照稳定后再允许平仓挂单")
        if utc_now_ms() < self.post_add_exit_block_until_ms:
            self.runtime.last_error = post_add_message
            return
        if self.runtime.last_error in post_add_messages:
            self.runtime.last_error = None
        target = self._planned_exit_target(plan_status)
        if target is None:
            self.exit_target_spread = None
            self.runtime.last_error = "等待真实均价、资金费和隔夜费数据后再计算平仓目标，不挂平仓单"
            return
        if self.runtime.last_error == "等待真实均价、资金费和隔夜费数据后再计算平仓目标，不挂平仓单":
            self.runtime.last_error = None
        self.exit_target_spread = target
        risk_exit_active = self._risk_exit_active(plan_status)
        risk_exit_reason = self._risk_exit_reason(plan_status) if risk_exit_active else None
        if risk_exit_active:
            if not self._risk_exit_trigger_confirmed(risk_exit_reason):
                return
        else:
            self._clear_risk_exit_confirm()
        if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
            current = quote.ask - mt4_quote.bid
            if current > target and not risk_exit_active:
                self.exit_ready_since_ms = 0
                self._clear_exit_confirm_message()
                return
            price = round_down(min(quote.bid - self.settings.binance_entry_offset_usd, mt4_quote.bid + target), self.binance.filters.tick_size)
            side = Side.BUY
        else:
            current = mt4_quote.ask - quote.bid
            if current > target and not risk_exit_active:
                self.exit_ready_since_ms = 0
                self._clear_exit_confirm_message()
                return
            price = round_up(max(quote.ask + self.settings.binance_entry_offset_usd, mt4_quote.ask - target), self.binance.filters.tick_size)
            side = Side.SELL
        guard_reason = self._exit_profit_guard_reason(pair, price, mt4_quote, plan_status, risk_exit_active)
        if guard_reason:
            self.exit_ready_since_ms = 0
            self.runtime.last_error = guard_reason
            return
        if not risk_exit_active and not self._exit_trigger_confirmed(current, target):
            return
        try:
            order = await self.binance.place_post_only_order(
                OrderRequest(symbol=self.settings.binance_symbol, side=side, quantity=pair.quantity_oz, price=price, reduce_only=True, position_side="SHORT" if side == Side.BUY else "LONG")
            )
        except BinanceError as exc:
            self.runtime.last_error = str(exc)[:240]
            self.storage.record_event("v2_exit_order_rejected", {"error": str(exc)[:160], "price": str(price)})
            return
        self.active_order = order
        self.active_exit_order_risk_active = risk_exit_active
        self.active_exit_order_risk_reason = risk_exit_reason
        self.order_created_ms = utc_now_ms()
        self.exit_ready_since_ms = 0
        self.runtime.state = StrategyState.QUOTING_BINANCE_EXIT
        self.storage.record_event(
            "v2_exit_order",
            {
                **order.model_dump(mode="json"),
                "exit_context": {
                    "risk_exit_active": risk_exit_active,
                    "risk_exit_reason": risk_exit_reason,
                    "planned_net": str(self._planned_exit_net(plan_status)) if self._planned_exit_net(plan_status) is not None else None,
                    "minimum_net": str(self._minimum_exit_net(pair)),
                },
            },
        )

    async def _check_exit_order(self, plan_status: dict[str, Any]) -> None:
        if not self.active_order:
            self.runtime.state = StrategyState.PAIR_OPEN if self.runtime.open_pair else StrategyState.IDLE
            return
        order = await self._refresh_active_order()
        if order.status in {OrderStatus.CANCELED, OrderStatus.EXPIRED} and order.executed_qty <= 0:
            self.storage.record_event("v2_exit_order_terminal", order.model_dump(mode="json"))
            self.active_order = None
            self._clear_active_order_context()
            self.runtime.state = StrategyState.PAIR_OPEN if self.runtime.open_pair else StrategyState.IDLE
            return
        if order.status == OrderStatus.REJECTED:
            self.storage.record_event("v2_exit_post_only_rejected", order.model_dump(mode="json"))
            self.active_order = None
            self._clear_active_order_context()
            self.runtime.state = StrategyState.PAIR_OPEN
            return
        if order.executed_qty <= 0:
            mt4_block_reason = self._mt4_trade_block_reason("平仓")
            if mt4_block_reason:
                self.runtime.last_error = mt4_block_reason
                await self.cancel_active_order(mt4_block_reason)
                return
        if order.status == OrderStatus.FILLED:
            self._schedule_binance_fill_audit(order, "exit")
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
        target = self._planned_exit_target(plan_status)
        if target is None:
            await self.cancel_active_order("V2 平仓目标数据未就绪，撤销未成交限价单")
            return
        binance_quote = self.binance.latest_quote()
        mt4_quote = self.mt4.latest_quote()
        gap_reason = xau_quote_gap_reason(binance_quote, mt4_quote)
        if gap_reason:
            await self.cancel_active_order(f"V2 平仓报价异常，撤销未成交限价单：{gap_reason}")
            return
        risk_exit_active = self._risk_exit_active(plan_status)
        if self.active_exit_order_risk_active and not risk_exit_active:
            await self.cancel_active_order("V2 风控平仓条件已解除，撤销未成交限价单")
            return
        guard_reason = self._exit_profit_guard_reason(pair, self.active_order.price, mt4_quote, plan_status, risk_exit_active)
        if guard_reason:
            await self.cancel_active_order(guard_reason)
            return
        if not exit_spread_ready(pair, binance_quote, mt4_quote, target) and not risk_exit_active:
            await self.cancel_active_order("V2 平仓价差回落，撤销未成交限价单")
            return
        if utc_now_ms() - self.order_created_ms > max(self.settings.min_order_live_ms, self.settings.max_order_age_ms):
            await self.cancel_active_order("V2 平仓限价单超时重挂")

    def _planned_exit_target(self, plan_status: dict[str, Any]) -> Decimal | None:
        exit_plan = (plan_status or {}).get("exit_plan") or {}
        if not exit_plan.get("enabled") or exit_plan.get("target_exit_spread") is None:
            return None
        return Decimal(str(exit_plan["target_exit_spread"]))

    def _loss_limit_active(self, plan_status: dict[str, Any]) -> bool:
        exit_plan = (plan_status or {}).get("exit_plan") or {}
        loss_limit = exit_plan.get("loss_limit") or {}
        return bool(loss_limit.get("active"))

    def _negative_swap_exit_active(self, plan_status: dict[str, Any]) -> bool:
        exit_plan = (plan_status or {}).get("exit_plan") or {}
        negative_swap = exit_plan.get("negative_swap") or {}
        return bool(negative_swap.get("active"))

    def _stale_weak_exit_active(self, plan_status: dict[str, Any]) -> bool:
        exit_plan = (plan_status or {}).get("exit_plan") or {}
        stale_weak = exit_plan.get("stale_weak") or {}
        return bool(stale_weak.get("active"))

    def _risk_exit_active(self, plan_status: dict[str, Any]) -> bool:
        return self._loss_limit_active(plan_status) or self._stale_weak_exit_active(plan_status) or self._negative_swap_exit_active(plan_status)

    def _risk_exit_reason(self, plan_status: dict[str, Any]) -> str | None:
        exit_plan = (plan_status or {}).get("exit_plan") or {}
        loss_limit = exit_plan.get("loss_limit") or {}
        if loss_limit.get("active"):
            return str(loss_limit.get("reason") or "最大亏损触发")
        stale_weak = exit_plan.get("stale_weak") or {}
        if stale_weak.get("active"):
            return str(stale_weak.get("reason") or "低质量旧仓受控释放")
        negative_swap = exit_plan.get("negative_swap") or {}
        if negative_swap.get("active"):
            return str(negative_swap.get("reason") or "负隔夜费风险触发")
        return None

    def _risk_exit_trigger_confirmed(self, reason: str | None) -> bool:
        confirm_ms = max(0, self.settings.risk_exit_confirm_ms)
        if confirm_ms == 0:
            return True
        now = utc_now_ms()
        marker = reason or "风控平仓触发"
        if self.risk_exit_confirm_reason != marker or self.risk_exit_ready_since_ms <= 0:
            self.risk_exit_confirm_reason = marker
            self.risk_exit_ready_since_ms = now
        elapsed = max(0, now - self.risk_exit_ready_since_ms)
        if elapsed >= confirm_ms:
            return True
        self.runtime.last_error = f"V2 风控平仓确认中 {elapsed}/{confirm_ms}ms：{marker}"
        return False

    def _clear_risk_exit_confirm(self) -> None:
        self.risk_exit_ready_since_ms = 0
        self.risk_exit_confirm_reason = None
        if self.runtime.last_error and self.runtime.last_error.startswith("V2 风控平仓确认中"):
            self.runtime.last_error = None

    def _clear_active_order_context(self) -> None:
        self.active_exit_order_risk_active = False
        self.active_exit_order_risk_reason = None
        self.active_mt4_follow_min_edge = None
        self._clear_risk_exit_confirm()

    def _exit_profit_guard_reason(
        self,
        pair: OpenPair,
        binance_exit_price: Decimal,
        mt4_quote,
        plan_status: dict[str, Any],
        risk_exit_active: bool,
    ) -> str | None:
        if risk_exit_active:
            return None
        min_net = self._minimum_exit_net(pair)
        plan_net = self._planned_exit_net(plan_status)
        if plan_net is None:
            return "V2 平仓利润保护：等待预估净值后再挂平仓单"
        if plan_net is not None and plan_net < min_net:
            return f"V2 平仓利润保护：计划净利 {plan_net} 低于最低 {min_net}，不挂平仓单"
        projected = self._projected_exit_net_at_limit(pair, binance_exit_price, mt4_quote, plan_status)
        if projected is None:
            return "V2 平仓利润保护：等待 MT4 可成交价后再评估平仓"
        if projected < min_net:
            return f"V2 平仓利润保护：限价复算净利 {projected} 低于最低 {min_net}，不平仓"
        return None

    def _planned_exit_net(self, plan_status: dict[str, Any]) -> Decimal | None:
        exit_plan = (plan_status or {}).get("exit_plan") or {}
        value = exit_plan.get("estimated_net")
        if value is None:
            return None
        return Decimal(str(value))

    def _minimum_exit_net(self, pair: OpenPair) -> Decimal:
        return max(Decimal("0"), self._effective_close_profit_usd_per_oz(pair) * pair.quantity_oz)

    def _effective_close_profit_usd_per_oz(self, pair: OpenPair) -> Decimal:
        if self._entry_spread_below_current_minimum(pair):
            return self._relaxed_close_profit_usd_per_oz()
        if self.settings.max_pair_age_minutes <= 0:
            return self.settings.close_profit_usd_per_oz
        age_ms = utc_now_ms() - int(pair.opened_ms)
        if age_ms >= self.settings.max_pair_age_minutes * 60_000:
            return self._relaxed_close_profit_usd_per_oz()
        return self.settings.close_profit_usd_per_oz

    def _entry_spread_below_current_minimum(self, pair: OpenPair) -> bool:
        entry_edge = self._pair_average_edge(pair)
        return entry_edge is not None and self.settings.open_min_edge > 0 and entry_edge < self.settings.open_min_edge

    def _relaxed_close_profit_usd_per_oz(self) -> Decimal:
        return max(Decimal("0"), min(self.settings.close_profit_usd_per_oz, self.settings.aged_close_profit_usd_per_oz))

    def _mt4_exit_guard_buffer_usd_per_oz(self) -> Decimal:
        point = self.mt4.latest_swap_info().point or Decimal("0.01")
        configured = Decimal(self.settings.mt4_slippage_points) * point + self.settings.mt4_close_extra_buffer_usd
        recent = self.mt4.recent_move_budget(min(self.settings.max_hedge_delay_ms, 1000), percentile=90, min_points=4)
        return configured + (recent or Decimal("0"))

    def _projected_exit_net_at_limit(
        self,
        pair: OpenPair,
        binance_exit_price: Decimal,
        mt4_quote,
        plan_status: dict[str, Any] | None = None,
    ) -> Decimal | None:
        if not mt4_quote:
            return None
        plan_projected = self._projected_exit_net_from_plan_at_limit(pair, binance_exit_price, mt4_quote, plan_status)
        if plan_projected is not None:
            return plan_projected
        qty = pair.quantity_oz
        if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
            mt4_exit_price = mt4_quote.bid
            binance_pnl = (pair.binance_entry_price - binance_exit_price) * qty
            mt4_pnl = (mt4_exit_price - pair.mt4_entry_price) * qty
        else:
            mt4_exit_price = mt4_quote.ask
            binance_pnl = (binance_exit_price - pair.binance_entry_price) * qty
            mt4_pnl = (pair.mt4_entry_price - mt4_exit_price) * qty
        return pair.realized_pnl + binance_pnl + mt4_pnl

    def _projected_exit_net_from_plan_at_limit(
        self,
        pair: OpenPair,
        binance_exit_price: Decimal,
        mt4_quote,
        plan_status: dict[str, Any] | None,
    ) -> Decimal | None:
        exit_plan = (plan_status or {}).get("exit_plan") or {}
        plan_net = self._planned_exit_net(plan_status or {})
        current_spread = self._plan_decimal(exit_plan, "current_exit_spread")
        limit_spread = self._limit_exit_spread(pair, binance_exit_price, mt4_quote)
        if plan_net is None or current_spread is None or limit_spread is None:
            return None
        return plan_net + ((current_spread - limit_spread) * pair.quantity_oz)

    def _limit_exit_spread(self, pair: OpenPair, binance_exit_price: Decimal, mt4_quote) -> Decimal | None:
        if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
            return binance_exit_price - mt4_quote.bid
        if pair.direction == PairDirection.BINANCE_LONG_MT4_SHORT:
            return mt4_quote.ask - binance_exit_price
        return None

    def _exit_trigger_confirmed(self, current: Decimal, target: Decimal) -> bool:
        confirm_ms = self.settings.effective_exit_confirm_ms
        if confirm_ms <= 0:
            return True
        now = utc_now_ms()
        if self.exit_ready_since_ms <= 0:
            self.exit_ready_since_ms = now
            self.storage.record_event(
                "v2_exit_trigger_confirming",
                {"current": str(current), "target": str(target), "elapsed_ms": 0, "confirm_ms": confirm_ms},
            )
        elapsed = now - self.exit_ready_since_ms
        if elapsed < confirm_ms:
            self.runtime.last_error = f"V2 平仓价差已触发，确认中 {elapsed}/{confirm_ms}ms，避免瞬时跳价假触发"
            return False
        if self.runtime.last_error and self.runtime.last_error.startswith("V2 平仓价差已触发，确认中"):
            self.runtime.last_error = None
        return True

    def _clear_exit_confirm_message(self) -> None:
        if self.runtime.last_error and self.runtime.last_error.startswith("V2 平仓价差已触发，确认中"):
            self.runtime.last_error = None

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
        if order is not None and order.executed_qty < pair.quantity_oz:
            self.storage.record_event(
                "v2_exit_binance_qty_short_waiting_repair",
                {
                    "pair_id": pair.pair_id,
                    "pair_quantity_oz": str(pair.quantity_oz),
                    "binance_exit_order": order.model_dump(mode="json"),
                },
            )
            self.active_order = None
            self.runtime.state = StrategyState.PAIR_OPEN
            self.runtime.last_error = (
                f"币安平仓成交 {order.executed_qty} XAU 小于组合 {pair.quantity_oz} XAU，"
                "禁止全平 MT4，等待下一轮实盘对账自动修复。"
            )
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
        self.close_mt4_qty = Decimal("0")
        self.close_mt4_notional = Decimal("0")
        mt4_quote = self.mt4.latest_quote()
        self.close_mt4_quote_bid = mt4_quote.bid if mt4_quote else None
        self.close_mt4_quote_ask = mt4_quote.ask if mt4_quote else None
        self.close_mt4_quote_ms = mt4_quote.timestamp_ms if mt4_quote else None
        self.close_mt4_report_ms = None
        self.close_started_ms = utc_now_ms()
        self.runtime.state = StrategyState.CLOSING_MT4
        self.storage.record_event(
            "v2_mt4_close_queued",
            {
                "binance_exit_order": order.model_dump(mode="json") if order else None,
                "command_ids": list(self.close_command_tickets.keys()),
                "tickets": list(self.pending_close_tickets),
                "lots_by_ticket": {str(ticket): str(lots_by_ticket[ticket]) for ticket in self.pending_close_tickets},
                "mt4_quote_at_command": {
                    "bid": str(self.close_mt4_quote_bid) if self.close_mt4_quote_bid is not None else None,
                    "ask": str(self.close_mt4_quote_ask) if self.close_mt4_quote_ask is not None else None,
                    "timestamp_ms": self.close_mt4_quote_ms,
                },
            },
        )

    def _close_lots_by_ticket(self, tickets: list[int], quantity_oz: Decimal) -> dict[int, Decimal]:
        positions = {position.ticket: position.lots for position in self.mt4.positions() if position.ticket in tickets}
        if positions:
            return {ticket: positions[ticket] for ticket in tickets if ticket in positions}
        per_ticket_qty = quantity_oz / Decimal(len(tickets))
        per_ticket_lots = lots_from_qty(self.settings, per_ticket_qty)
        return {ticket: per_ticket_lots for ticket in tickets}

    def start_binance_restore(self, order: OrderUpdate, existing_qty: Decimal) -> None:
        self.active_order = order
        self._clear_active_order_context()
        self.order_created_ms = utc_now_ms()
        self.repairing_binance_only = True
        self.repair_existing_qty = max(Decimal("0"), existing_qty)
        self.runtime.state = StrategyState.QUOTING_BINANCE_ENTRY
        self.runtime.last_error = "币安持仓数量偏少，已挂 Post Only 同向补齐单；MT4 不重复跟随。"
        self.storage.record_event(
            "v2_binance_restore_order_started",
            {"existing_qty": str(existing_qty), **order.model_dump(mode="json")},
        )

    async def _check_binance_restore_order(self) -> None:
        if not self.active_order:
            self.repairing_binance_only = False
            self.repair_existing_qty = Decimal("0")
            self.runtime.state = StrategyState.PAIR_OPEN if self.runtime.open_pair else StrategyState.IDLE
            return
        order = await self._refresh_active_order()
        if order.status in {OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED} and order.executed_qty <= 0:
            self.storage.record_event("v2_binance_restore_order_terminal", order.model_dump(mode="json"))
            self.active_order = None
            self.repairing_binance_only = False
            self.repair_existing_qty = Decimal("0")
            self.runtime.state = StrategyState.PAIR_OPEN if self.runtime.open_pair else StrategyState.IDLE
            return
        if order.executed_qty > 0 and order.status in TERMINAL:
            self._complete_binance_restore(order)
            return
        if order.status == OrderStatus.FILLED:
            self._complete_binance_restore(order)
            return
        if order.executed_qty > 0:
            self.runtime.last_error = f"币安缺口补齐单已部分成交 {order.executed_qty}/{order.orig_qty} XAU，继续等待完全成交。"
            return
        if utc_now_ms() - self.order_created_ms > max(self.settings.min_order_live_ms, self.settings.max_order_age_ms):
            await self.cancel_active_order("币安缺口补齐限价单超时重挂")

    def _complete_binance_restore(self, order: OrderUpdate) -> None:
        pair = self.runtime.open_pair
        if not pair:
            self.storage.record_event("v2_binance_restore_without_pair", order.model_dump(mode="json"))
            self.active_order = None
            self.repairing_binance_only = False
            self.repair_existing_qty = Decimal("0")
            self.runtime.state = StrategyState.IDLE
            self.runtime.last_error = "币安缺口补齐成交但组合记录缺失，等待实盘对账。"
            return
        filled_qty = max(Decimal("0"), order.executed_qty)
        used_qty = min(filled_qty, max(Decimal("0"), pair.quantity_oz - self.repair_existing_qty))
        new_qty = self.repair_existing_qty + used_qty
        updates: dict[str, Any] = {}
        if used_qty > 0 and new_qty > 0 and order.avg_price > 0:
            updates["binance_entry_price"] = ((pair.binance_entry_price * self.repair_existing_qty) + (order.avg_price * used_qty)) / new_qty
            updates["binance_order_id"] = f"{pair.binance_order_id} / {order.order_id}"
        if updates:
            self.runtime.open_pair = pair.model_copy(update=updates)
        self.storage.record_event(
            "v2_binance_restore_completed",
            {
                "pair_id": pair.pair_id,
                "existing_qty": str(self.repair_existing_qty),
                "filled_qty": str(filled_qty),
                "used_qty": str(used_qty),
                "target_qty": str(pair.quantity_oz),
                "order": order.model_dump(mode="json"),
            },
        )
        self.active_order = None
        self.repairing_binance_only = False
        self.repair_existing_qty = Decimal("0")
        self.runtime.state = StrategyState.PAIR_OPEN
        self.runtime.last_error = None if new_qty >= pair.quantity_oz else "币安缺口已部分补齐，等待下一轮对账继续补齐。"

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
        self._record_mt4_entry_slippage("v2_mt4_entry_slippage", report)
        edge = self.active_order.avg_price - report.fill_price if self.entry_direction == PairDirection.BINANCE_SHORT_MT4_LONG else report.fill_price - self.active_order.avg_price
        self.runtime.open_pair = OpenPair(direction=self.entry_direction, quantity_oz=self.active_order.executed_qty, binance_entry_price=self.active_order.avg_price, mt4_entry_price=report.fill_price, binance_order_id=self.active_order.order_id, mt4_ticket=report.ticket, mt4_tickets=[report.ticket] if report.ticket else [], base_edge=edge)
        self.last_entry_ms = int(self.runtime.open_pair.opened_ms)
        self.storage.record_event("v2_pair_open", self.runtime.open_pair.model_dump(mode="json"))
        self.post_add_exit_block_until_ms = utc_now_ms() + max(5000, self.settings.max_hedge_delay_ms)
        self.active_order = None
        self.hedge_command_id = None
        self._clear_entry_quote()
        self._clear_entry_carry()
        self.active_mt4_follow_min_edge = None
        self.runtime.state = StrategyState.PAIR_OPEN
        self.runtime.last_error = None

    def _capture_entry_mt4_quote(self) -> dict[str, str | int | None]:
        quote = self.mt4.latest_quote()
        self.entry_mt4_quote_bid = quote.bid if quote else None
        self.entry_mt4_quote_ask = quote.ask if quote else None
        self.entry_mt4_quote_ms = quote.timestamp_ms if quote else None
        return {
            "bid": str(self.entry_mt4_quote_bid) if self.entry_mt4_quote_bid is not None else None,
            "ask": str(self.entry_mt4_quote_ask) if self.entry_mt4_quote_ask is not None else None,
            "timestamp_ms": self.entry_mt4_quote_ms,
        }

    def _record_mt4_entry_slippage(self, kind: str, report) -> None:
        adverse = None
        reference = None
        if report.fill_price is not None and self.entry_hedge_side == Side.BUY and self.entry_mt4_quote_ask is not None:
            reference = self.entry_mt4_quote_ask
            adverse = report.fill_price - self.entry_mt4_quote_ask
        elif report.fill_price is not None and self.entry_hedge_side == Side.SELL and self.entry_mt4_quote_bid is not None:
            reference = self.entry_mt4_quote_bid
            adverse = self.entry_mt4_quote_bid - report.fill_price
        self.storage.record_event(
            kind,
            {
                "command_id": report.command_id,
                "side": self.entry_hedge_side.value if self.entry_hedge_side else None,
                "fill_price": str(report.fill_price) if report.fill_price is not None else None,
                "reference_price": str(reference) if reference is not None else None,
                "mt4_entry_adverse_slippage": str(adverse) if adverse is not None else None,
                "mt4_quote_at_command": {
                    "bid": str(self.entry_mt4_quote_bid) if self.entry_mt4_quote_bid is not None else None,
                    "ask": str(self.entry_mt4_quote_ask) if self.entry_mt4_quote_ask is not None else None,
                    "timestamp_ms": self.entry_mt4_quote_ms,
                },
                "mt4_report_timestamp_ms": report.timestamp_ms,
                "mt4_command_to_report_latency_ms": (
                    max(0, int(report.timestamp_ms) - self.hedge_started_ms)
                    if report.timestamp_ms and self.hedge_started_ms
                    else None
                ),
            },
        )

    def _clear_entry_quote(self) -> None:
        self.entry_mt4_quote_bid = None
        self.entry_mt4_quote_ask = None
        self.entry_mt4_quote_ms = None

    def _entry_interval_blocked(self) -> bool:
        interval_ms = max(0, self.settings.gold_v2_min_entry_interval_ms)
        if interval_ms <= 0 or self.last_entry_ms <= 0:
            return False
        elapsed = utc_now_ms() - self.last_entry_ms
        if elapsed >= interval_ms:
            return False
        minutes_left = max(1, (interval_ms - elapsed + 59_999) // 60_000)
        self.runtime.last_error = f"V2 开仓频率控制中，约 {minutes_left} 分钟后再允许新首仓；已有持仓的补仓和平仓不受影响。"
        return True

    def _load_last_entry_ms(self) -> int:
        try:
            now = utc_now_ms()
            events = self.storage.get_events(now - 7 * 24 * 60 * 60 * 1000, now + 1000, limit=2000)
        except Exception:  # noqa: BLE001
            return 0
        for event in reversed(events):
            if event.get("kind") != "v2_pair_open":
                continue
            payload = event.get("payload") or {}
            try:
                opened_ms = int(payload.get("opened_ms") or 0)
            except (TypeError, ValueError):
                continue
            if opened_ms >= GOLD_V2_CURRENT_GUARD_START_MS:
                return opened_ms
        return 0

    def _handle_close_report(self, report) -> None:
        if report.status != "ok":
            ticket = report.ticket or self.close_command_tickets.get(report.command_id)
            self.close_command_tickets.pop(report.command_id, None)
            if report.error_code in MT4_MARKET_CLOSED_ERROR_CODES:
                self._handle_mt4_close_market_closed(ticket, f"MT4 平仓失败：{report.message or report.error_code}")
                return
            self._retry_mt4_close_ticket(ticket, f"MT4 平仓跟随失败：{report.message or report.error_code}")
            return
        ticket = report.ticket or self.close_command_tickets.get(report.command_id)
        pair_id = self.runtime.open_pair.pair_id if self.runtime.open_pair else None
        if ticket is not None:
            self.pending_close_tickets.discard(ticket)
        if report.fill_price is not None and report.lots > 0:
            closed_qty = report.lots * self.settings.mt4_lot_size_oz
            self.close_mt4_qty += closed_qty
            self.close_mt4_notional += report.fill_price * closed_qty
            self.close_mt4_report_ms = report.timestamp_ms
        self.close_command_tickets.pop(report.command_id, None)
        self.storage.record_event("v2_pair_close_ticket", {"pair_id": pair_id, "ticket": ticket})
        if self.pending_close_tickets:
            return
        pair = self.runtime.open_pair
        tickets = pair.mt4_tickets if pair else []
        if pair:
            self._record_closed_pair_pnl(pair, self.active_order)
        self.storage.record_event("v2_pair_closed", {"pair_id": pair_id, "tickets": tickets})
        self.runtime.open_pair = None
        self.active_order = None
        self.close_command_id = None
        self.close_command_tickets = {}
        self.pending_close_tickets = set()
        self.close_mt4_qty = Decimal("0")
        self.close_mt4_notional = Decimal("0")
        self.close_mt4_quote_bid = None
        self.close_mt4_quote_ask = None
        self.close_mt4_quote_ms = None
        self.close_mt4_report_ms = None
        self._clear_exit_carry()
        self.last_closed_ms = utc_now_ms()
        self.runtime.state = StrategyState.IDLE
        self.runtime.last_error = None

    def _schedule_binance_fill_audit(self, order: OrderUpdate, phase: str) -> None:
        if self.settings.is_dry_run:
            return
        try:
            asyncio.create_task(self._audit_binance_fill(order, phase))
        except RuntimeError:
            self.storage.record_event(
                "v2_binance_fill_audit_not_scheduled",
                {"phase": phase, "order_id": order.order_id, "reason": "event loop unavailable"},
            )

    async def _audit_binance_fill(self, order: OrderUpdate, phase: str) -> None:
        start_ms = max(0, int(order.timestamp_ms) - 10 * 60_000)
        end_ms = utc_now_ms() + 60_000
        try:
            rows = await self.binance.user_trades(start_ms, end_ms, limit=1000)
        except Exception as exc:  # noqa: BLE001
            self.storage.record_event(
                "v2_binance_fill_audit_failed",
                {"phase": phase, "order_id": order.order_id, "error": str(exc)[:160]},
            )
            return
        matched = [row for row in rows if str(row.get("orderId") or row.get("order_id") or "") == str(order.order_id)]
        if not matched:
            self.storage.record_event(
                "v2_binance_fill_audit_pending",
                {"phase": phase, "order_id": order.order_id, "checked_rows": len(rows)},
            )
            return
        commission = sum((_decimal_or_zero(row.get("commission")) for row in matched), Decimal("0"))
        non_maker_rows = [row for row in matched if not _trade_is_maker(row)]
        payload = {
            "phase": phase,
            "order_id": order.order_id,
            "trade_count": len(matched),
            "commission": str(commission),
            "commission_asset": _join_unique_text(row.get("commissionAsset") or row.get("commission_asset") for row in matched),
            "all_maker": not non_maker_rows,
            "non_maker_trade_ids": [str(row.get("id") or row.get("tradeId") or "") for row in non_maker_rows],
        }
        self.storage.record_event("v2_binance_fill_audit", payload)
        if commission != 0 or non_maker_rows:
            self.storage.record_event("v2_binance_fee_or_taker_detected", payload)

    def _handle_mt4_close_market_closed(self, ticket: int | None, reason: str) -> None:
        pair = self.runtime.open_pair
        if pair and self.active_order and self.active_order.executed_qty > 0:
            self._record_binance_exit_without_mt4(pair, self.active_order, reason)
        self.mt4_exit_block_until_ms = utc_now_ms() + MT4_EXIT_BLOCK_MS
        self.close_command_id = None
        self.close_command_tickets = {}
        self.pending_close_tickets = set()
        self.close_mt4_qty = Decimal("0")
        self.close_mt4_notional = Decimal("0")
        self.close_mt4_report_ms = None
        self.active_order = None
        self.runtime.state = StrategyState.PAIR_OPEN if self.runtime.open_pair else StrategyState.IDLE
        self.runtime.last_error = self._mt4_exit_block_message()
        self.storage.record_event(
            "v2_mt4_market_closed_exit_blocked",
            {
                "ticket": ticket,
                "reason": reason,
                "block_until_ms": self.mt4_exit_block_until_ms,
            },
        )

    def _record_binance_exit_without_mt4(self, pair: OpenPair, order: OrderUpdate, reason: str) -> None:
        qty = order.executed_qty
        if qty <= 0 or order.avg_price <= 0:
            return
        if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
            binance_pnl = (pair.binance_entry_price - order.avg_price) * qty
        else:
            binance_pnl = (order.avg_price - pair.binance_entry_price) * qty
        realized = pair.realized_pnl + binance_pnl
        self.runtime.open_pair = pair.model_copy(update={"realized_pnl": realized})
        self.storage.record_event(
            "v2_binance_exit_without_mt4_recorded",
            {
                "pair_id": pair.pair_id,
                "reason": reason,
                "order_id": order.order_id,
                "qty": str(qty),
                "binance_entry_price": str(pair.binance_entry_price),
                "binance_exit_price": str(order.avg_price),
                "binance_pnl": str(binance_pnl),
                "realized_pnl": str(realized),
            },
        )

    def _mt4_exit_block_reason(self) -> str | None:
        now = utc_now_ms()
        if self.mt4_exit_block_until_ms > now:
            return self._mt4_exit_block_message()
        if self.mt4_exit_block_until_ms > 0:
            self.mt4_exit_block_until_ms = 0
            if self._last_error_is_mt4_close_failure():
                self.runtime.last_error = None
            return None
        if not self.mt4_exit_block_seeded_from_last_error and self._last_error_is_mt4_close_failure():
            self.mt4_exit_block_seeded_from_last_error = True
            self.mt4_exit_block_until_ms = now + MT4_EXIT_BLOCK_MS
            self.storage.record_event(
                "v2_mt4_exit_block_seeded_from_last_error",
                {"last_error": self.runtime.last_error, "block_until_ms": self.mt4_exit_block_until_ms},
            )
            return self._mt4_exit_block_message()
        return None

    def _last_error_is_mt4_close_failure(self) -> bool:
        text = self.runtime.last_error or ""
        return (
            "MT4 平仓跟随失败" in text
            or "MT4 平仓失败" in text
            or "OrderClose failed" in text
            or "MT4 暂不可平仓" in text
            or "MT4 暂不可交易" in text
        )

    def _mt4_trade_block_reason(self, action: str) -> str | None:
        if not self.settings.is_dry_run:
            if action in {"开仓", "补仓"}:
                weekend_reason = xau_weekend_entry_block_reason(utc_now_ms())
                if weekend_reason:
                    return f"{weekend_reason}，已禁止币安{action}挂单，已有持仓仍允许按策略平仓。"
            ea_version = self.mt4.ea_version()
            if ea_version != REQUIRED_MT4_EA_VERSION:
                return (
                    f"MT4 EA版本 {ea_version or '未上报'} 不是 {REQUIRED_MT4_EA_VERSION}，"
                    f"已禁止币安{action}挂单并保持当前对冲。"
                )
            trade_allowed = self.mt4.trade_allowed()
            if trade_allowed is not True:
                return f"MT4 交易状态未确认可交易，已禁止币安{action}挂单并保持当前对冲。"
            if self.mt4.trade_context_busy():
                return f"MT4 交易通道忙，已禁止币安{action}挂单并保持当前对冲。"
        if self._mt4_exit_block_reason():
            return f"MT4 暂不可交易，已禁止币安{action}挂单并保持当前对冲。"
        return None

    def _mt4_exit_block_message(self) -> str:
        return "MT4 暂不可平仓，已禁止币安平仓挂单并保持对冲，等待 MT4 恢复交易后再尝试离场。"

    def _record_closed_pair_pnl(self, pair: OpenPair, exit_order: OrderUpdate | None) -> None:
        if not exit_order or exit_order.executed_qty <= 0 or exit_order.avg_price <= 0 or self.close_mt4_qty <= 0:
            self.storage.record_event(
                "v2_pair_pnl_not_recorded",
                {
                    "pair_id": pair.pair_id,
                    "reason": "缺少币安平仓均价或MT4平仓回报",
                    "binance_exit_order": exit_order.model_dump(mode="json") if exit_order else None,
                    "mt4_close_qty": str(self.close_mt4_qty),
                },
            )
            return
        qty = min(pair.quantity_oz, exit_order.executed_qty, self.close_mt4_qty)
        mt4_exit_price = self.close_mt4_notional / self.close_mt4_qty
        if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
            binance_pnl = (pair.binance_entry_price - exit_order.avg_price) * qty
            mt4_pnl = (mt4_exit_price - pair.mt4_entry_price) * qty
            entry_spread = pair.binance_entry_price - pair.mt4_entry_price
            actual_exit_spread = exit_order.avg_price - mt4_exit_price
            mt4_close_quote = self.close_mt4_quote_bid
            mt4_adverse_slippage = (self.close_mt4_quote_bid - mt4_exit_price) if self.close_mt4_quote_bid is not None else None
        else:
            binance_pnl = (exit_order.avg_price - pair.binance_entry_price) * qty
            mt4_pnl = (pair.mt4_entry_price - mt4_exit_price) * qty
            entry_spread = pair.mt4_entry_price - pair.binance_entry_price
            actual_exit_spread = mt4_exit_price - exit_order.avg_price
            mt4_close_quote = self.close_mt4_quote_ask
            mt4_adverse_slippage = (mt4_exit_price - self.close_mt4_quote_ask) if self.close_mt4_quote_ask is not None else None
        realized = pair.realized_pnl + binance_pnl + mt4_pnl
        binance_to_mt4_latency_ms = None
        if self.close_mt4_report_ms is not None and exit_order.timestamp_ms:
            binance_to_mt4_latency_ms = max(0, self.close_mt4_report_ms - exit_order.timestamp_ms)
        command_to_report_latency_ms = None
        if self.close_mt4_report_ms is not None and self.close_started_ms:
            command_to_report_latency_ms = max(0, self.close_mt4_report_ms - self.close_started_ms)
        self.storage.record_pnl(pair.pair_id, realized)
        self.storage.record_event(
            "v2_pair_pnl_recorded",
            {
                "pair_id": pair.pair_id,
                "realized_pnl": str(realized),
                "binance_pnl": str(binance_pnl),
                "mt4_pnl": str(mt4_pnl),
                "qty": str(qty),
                "entry_spread": str(entry_spread),
                "actual_exit_spread": str(actual_exit_spread),
                "binance_exit_price": str(exit_order.avg_price),
                "mt4_exit_price": str(mt4_exit_price),
                "mt4_close_quote": str(mt4_close_quote) if mt4_close_quote is not None else None,
                "mt4_close_adverse_slippage": str(mt4_adverse_slippage) if mt4_adverse_slippage is not None else None,
                "binance_exit_timestamp_ms": exit_order.timestamp_ms,
                "mt4_close_command_timestamp_ms": self.close_started_ms or None,
                "mt4_close_quote_timestamp_ms": self.close_mt4_quote_ms,
                "mt4_close_report_timestamp_ms": self.close_mt4_report_ms,
                "binance_to_mt4_latency_ms": binance_to_mt4_latency_ms,
                "mt4_command_to_report_latency_ms": command_to_report_latency_ms,
            },
        )

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


def _decimal_or_zero(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _trade_is_maker(row: dict[str, Any]) -> bool:
    value = row.get("maker")
    if value is None:
        value = row.get("isMaker")
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"true", "1", "yes"}


def _join_unique_text(values) -> str | None:
    seen = []
    for value in values:
        if value is None:
            continue
        text = str(value)
        if text and text not in seen:
            seen.append(text)
    return " / ".join(seen) if seen else None
