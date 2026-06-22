from __future__ import annotations

import logging
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from app.binance_client import BinanceBaseClient, BinanceError
from app.config import Settings
from app.models import (
    EntryPlan,
    ExchangeFilters,
    MarketQuote,
    OpenPair,
    OrderRequest,
    OrderStatus,
    OrderUpdate,
    PairDirection,
    Side,
    StrategyState,
    utc_now_ms,
)
from app.mt4_bridge import Mt4Bridge
from app.risk import RiskManager
from app.storage import Storage


logger = logging.getLogger(__name__)
TERMINAL_ORDER_STATUSES = {OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED, OrderStatus.FILLED}


def round_down(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def round_up(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def build_entry_plan(
    settings: Settings,
    filters: ExchangeFilters,
    binance: MarketQuote,
    mt4: MarketQuote,
) -> EntryPlan | None:
    qty = max(round_down(settings.target_oz, filters.qty_step), filters.min_qty)
    edge_high = binance.ask - mt4.ask
    if edge_high >= settings.open_min_edge:
        price = round_up(
            max(binance.ask + settings.binance_entry_offset_usd, mt4.ask + settings.open_min_edge),
            filters.tick_size,
        )
        return EntryPlan(
            direction=PairDirection.BINANCE_SHORT_MT4_LONG,
            binance_side=Side.SELL,
            limit_price=price,
            quantity_oz=qty,
            edge=edge_high,
            mt4_hedge_side=Side.BUY,
            mt4_price_limit=price - settings.min_locked_edge,
        )
    edge_low = mt4.bid - binance.bid
    if edge_low >= settings.open_min_edge:
        price = round_down(
            min(binance.bid - settings.binance_entry_offset_usd, mt4.bid - settings.open_min_edge),
            filters.tick_size,
        )
        return EntryPlan(
            direction=PairDirection.BINANCE_LONG_MT4_SHORT,
            binance_side=Side.BUY,
            limit_price=price,
            quantity_oz=qty,
            edge=edge_low,
            mt4_hedge_side=Side.SELL,
            mt4_price_limit=price + settings.min_locked_edge,
        )
    return None


def build_directional_entry_plan(
    settings: Settings,
    filters: ExchangeFilters,
    binance: MarketQuote,
    mt4: MarketQuote,
    direction: PairDirection,
    min_edge: Decimal,
) -> EntryPlan | None:
    qty = max(round_down(settings.target_oz, filters.qty_step), filters.min_qty)
    if direction == PairDirection.BINANCE_SHORT_MT4_LONG:
        edge = binance.ask - mt4.ask
        if edge < min_edge:
            return None
        price = round_up(
            max(binance.ask + settings.binance_entry_offset_usd, mt4.ask + min_edge),
            filters.tick_size,
        )
        return EntryPlan(
            direction=direction,
            binance_side=Side.SELL,
            limit_price=price,
            quantity_oz=qty,
            edge=edge,
            mt4_hedge_side=Side.BUY,
            mt4_price_limit=price - settings.min_locked_edge,
        )
    edge = mt4.bid - binance.bid
    if edge < min_edge:
        return None
    price = round_down(
        min(binance.bid - settings.binance_entry_offset_usd, mt4.bid - min_edge),
        filters.tick_size,
    )
    return EntryPlan(
        direction=direction,
        binance_side=Side.BUY,
        limit_price=price,
        quantity_oz=qty,
        edge=edge,
        mt4_hedge_side=Side.SELL,
        mt4_price_limit=price + settings.min_locked_edge,
    )


def _binance_order_missing(exc: BinanceError) -> bool:
    text = str(exc)
    return "-2013" in text or "-2011" in text or "Order does not exist" in text or "Unknown order" in text


class StrategyEngine:
    def __init__(
        self,
        settings: Settings,
        binance: BinanceBaseClient,
        mt4: Mt4Bridge,
        risk: RiskManager,
        storage: Storage,
    ) -> None:
        self.settings = settings
        self.binance = binance
        self.mt4 = mt4
        self.risk = risk
        self.storage = storage
        self.state = StrategyState.IDLE
        self.candidate_plan: EntryPlan | None = None
        self.candidate_started_ms = 0
        self.last_entry_cancel_ms = 0
        self.active_plan: EntryPlan | None = None
        self.active_order: OrderUpdate | None = None
        self.order_created_ms = 0
        self.hedge_started_ms = 0
        self.pending_hedge_qty = Decimal("0")
        self.hedged_qty = Decimal("0")
        self.open_pair: OpenPair | None = None
        self.adding_to_pair = False
        self.pending_close_tickets: set[int] = set()
        self.pending_close_commands: dict[str, int] = {}
        self.exit_force_reason: str | None = None
        self.last_error: str | None = None
        self._close_trigger_cache_ms = 0
        self._close_trigger_cache: Decimal | None = None

    async def step(self) -> None:
        await self._handle_mt4_reports()
        binance_quote = self.binance.latest_quote()
        mt4_quote = self.mt4.latest_quote()
        if self.state == StrategyState.PAUSED:
            if not self._can_resume_pair_after_transient_quote_pause(binance_quote, mt4_quote):
                return
            self.state = StrategyState.PAIR_OPEN
        if self.state == StrategyState.IDLE:
            if binance_quote is None or mt4_quote is None:
                self.last_error = None
                return
            if not self._quotes_fresh(binance_quote, mt4_quote):
                return
            await self._maybe_enter(binance_quote, mt4_quote)
        elif self.state == StrategyState.QUOTING_BINANCE_ENTRY:
            if not self._quotes_fresh(binance_quote, mt4_quote):
                await self._cancel_stale_entry_quote()
                return
            await self._check_entry_order()
        elif self.state == StrategyState.HEDGING_MT4:
            await self._check_hedge_timeout()
        elif self.state == StrategyState.PAIR_OPEN:
            if not self._quotes_fresh(binance_quote, mt4_quote):
                return
            force_exit_reason = self._negative_swap_exit_reason()
            if force_exit_reason:
                await self._maybe_exit(binance_quote, mt4_quote, force=True, reason=force_exit_reason)
                return
            if await self._maybe_add_position(binance_quote, mt4_quote):
                return
            await self._maybe_exit(binance_quote, mt4_quote)
        elif self.state == StrategyState.QUOTING_BINANCE_EXIT:
            await self._check_exit_order()

    def _can_resume_pair_after_transient_quote_pause(
        self,
        binance_quote: MarketQuote | None,
        mt4_quote: MarketQuote | None,
    ) -> bool:
        if not self.open_pair or not self._paused_for_quote_issue():
            return False
        return self._quotes_fresh(binance_quote, mt4_quote)

    def _paused_for_quote_issue(self) -> bool:
        if not self.last_error:
            return False
        return self.last_error == "quote missing" or self.last_error.startswith("quote stale ")

    def resume(self) -> None:
        if self.state != StrategyState.PAUSED:
            return
        if self.open_pair:
            self.active_plan = None
            self.active_order = None
            self.pending_hedge_qty = Decimal("0")
            self.state = StrategyState.PAIR_OPEN
        else:
            self._clear_entry()
        self.last_error = None

    def clear_runtime_state(self) -> None:
        self._reset_all()
        self.last_error = None

    def entry_candidate_age_ms(self) -> int:
        if not self.candidate_plan or self.candidate_started_ms <= 0:
            return 0
        return max(0, utc_now_ms() - self.candidate_started_ms)

    async def _maybe_enter(self, binance_quote: MarketQuote | None, mt4_quote: MarketQuote | None) -> None:
        if not binance_quote or not mt4_quote:
            return
        if self.last_entry_cancel_ms and utc_now_ms() - self.last_entry_cancel_ms < self.settings.requote_cooldown_ms:
            return
        if not self.settings.is_dry_run:
            live_check = self.risk.live_ready(
                binance_ready=binance_quote is not None,
                mt4_connected=self.mt4.connected(),
                maker_fee_loaded=self.binance.maker_fee_rate is not None,
            )
            if not live_check.ok:
                self.last_error = live_check.reason
                return
        plan = build_entry_plan(self.settings, self.binance.filters, binance_quote, mt4_quote)
        if not plan:
            self._clear_entry_candidate()
            return
        if not self._entry_candidate_ready(plan):
            return
        if not await self._live_entry_guard_ok():
            self._clear_entry_candidate()
            return
        order = await self.binance.place_post_only_order(
            OrderRequest(
                symbol=self.settings.binance_symbol,
                side=plan.binance_side,
                quantity=plan.quantity_oz,
                price=plan.limit_price,
                post_only=True,
            )
        )
        self.storage.record_event("entry_order", order.model_dump(mode="json"))
        self._clear_entry_candidate()
        if order.status == OrderStatus.REJECTED:
            self.last_entry_cancel_ms = utc_now_ms()
            return
        self.active_plan = plan
        self.active_order = order
        self.order_created_ms = utc_now_ms()
        self.state = StrategyState.QUOTING_BINANCE_ENTRY

    async def _check_entry_order(self) -> None:
        if not self.active_order or not self.active_plan:
            self.state = StrategyState.IDLE
            return
        entry_max_age_ms = max(self.settings.max_order_age_ms, self.settings.min_order_live_ms)
        if utc_now_ms() - self.order_created_ms > entry_max_age_ms:
            order = await self._cancel_entry_order(self.active_order)
            if order is None:
                return
            if await self._handle_entry_cancel_fill(order, "币安开仓挂单超时，撤单时发现已有成交", allow_mt4_hedge=True):
                return
            self._clear_entry()
            return
        order = await self._refresh_entry_order()
        if not order:
            return
        self.active_order = order
        if order.status == OrderStatus.REJECTED:
            self._clear_entry()
            return
        if order.status in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED} and self.active_plan:
            if not self._entry_plan_still_valid() and self._entry_order_can_cancel_for_spread():
                order = await self._cancel_entry_order(order)
                if order is None:
                    return
                if await self._handle_entry_cancel_fill(order, "币安开仓价差失效，撤单时发现已有成交", allow_mt4_hedge=True):
                    return
                if order.executed_qty <= self.hedged_qty + self.pending_hedge_qty:
                    self._clear_entry()
                    return
        if order.executed_qty > self.hedged_qty + self.pending_hedge_qty:
            await self._queue_mt4_hedge(order.executed_qty - self.hedged_qty - self.pending_hedge_qty, order.avg_price)
        if order.status == OrderStatus.CANCELED and order.executed_qty == self.hedged_qty:
            self._clear_entry()

    def _entry_plan_still_valid(self) -> bool:
        if not self.active_plan:
            return False
        binance_quote = self.binance.latest_quote()
        mt4_quote = self.mt4.latest_quote()
        if not binance_quote or not mt4_quote:
            return False
        if self.adding_to_pair and self.open_pair:
            base_edge = self._last_add_edge()
            if base_edge is None:
                return False
            min_edge = base_edge + self.settings.add_edge_growth_usd
            return self._current_edge(self.active_plan.direction, binance_quote, mt4_quote) >= min_edge
        if self.active_plan.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
            return binance_quote.ask - mt4_quote.ask >= self.settings.cancel_min_edge
        return mt4_quote.bid - binance_quote.bid >= self.settings.cancel_min_edge

    def _entry_order_can_cancel_for_spread(self) -> bool:
        if self.settings.min_order_live_ms <= 0:
            return True
        return utc_now_ms() - self.order_created_ms >= self.settings.min_order_live_ms

    def _entry_candidate_ready(self, plan: EntryPlan) -> bool:
        now = utc_now_ms()
        if not self.candidate_plan or self.candidate_plan.direction != plan.direction:
            self.candidate_plan = plan
            self.candidate_started_ms = now
            return self.settings.entry_confirm_ms <= 0
        self.candidate_plan = plan
        return now - self.candidate_started_ms >= self.settings.entry_confirm_ms

    def _clear_entry_candidate(self) -> None:
        self.candidate_plan = None
        self.candidate_started_ms = 0

    async def _cancel_stale_entry_quote(self) -> None:
        reason = self.last_error or "quote stale during entry"
        order = await self._refresh_entry_order()
        if order:
            order = await self._cancel_entry_order(order)
            if order is None:
                return
            if await self._handle_entry_cancel_fill(order, reason, allow_mt4_hedge=False):
                return
        self.storage.record_event("entry_quote_stale_cancel", {"reason": reason, "order_id": order.order_id if order else None})
        self._clear_entry()
        self.last_error = reason

    async def _refresh_entry_order(self) -> OrderUpdate | None:
        if not self.active_order:
            return None
        try:
            order = await self.binance.get_order(self.active_order.order_id)
        except BinanceError as exc:
            if _binance_order_missing(exc):
                age_ms = utc_now_ms() - self.order_created_ms
                self.storage.record_event(
                    "entry_order_not_visible",
                    {"order_id": self.active_order.order_id, "age_ms": age_ms, "error": str(exc)[:160]},
                )
                if age_ms > self.settings.max_order_age_ms + 10000:
                    self.state = StrategyState.PAUSED
                    self.last_error = "币安挂单长时间查询不到，已暂停，需人工确认币安挂单/持仓"
                    return None
                return None
            raise
        if order:
            self.active_order = order
        return order

    async def _cancel_entry_order(self, order: OrderUpdate) -> OrderUpdate | None:
        if order.status in TERMINAL_ORDER_STATUSES:
            self.active_order = order
            return order
        try:
            canceled = await self.binance.cancel_order(order.order_id)
            if canceled:
                self.active_order = canceled
                return canceled
        except BinanceError as exc:
            if _binance_order_missing(exc):
                self.storage.record_event("entry_order_cancel_not_visible", {"order_id": order.order_id, "error": str(exc)[:160]})
                return None
            raise
        try:
            refreshed = await self.binance.get_order(order.order_id)
            if refreshed:
                self.active_order = refreshed
                return refreshed
        except BinanceError as exc:
            if not _binance_order_missing(exc):
                raise
            self.storage.record_event("entry_order_missing_after_cancel", {"order_id": order.order_id, "error": str(exc)[:160]})
        return None

    async def _handle_entry_cancel_fill(self, order: OrderUpdate, reason: str, allow_mt4_hedge: bool) -> bool:
        unhedged_qty = order.executed_qty - self.hedged_qty - self.pending_hedge_qty
        if unhedged_qty <= 0:
            return False
        self.active_order = order
        self.storage.record_event(
            "entry_cancel_race_fill",
            {
                "reason": reason,
                "order_id": order.order_id,
                "side": order.side.value,
                "executed_qty": str(order.executed_qty),
                "unhedged_qty": str(unhedged_qty),
                "avg_price": str(order.avg_price),
                "allow_mt4_hedge": allow_mt4_hedge,
            },
        )
        if allow_mt4_hedge:
            await self._queue_mt4_hedge(unhedged_qty, order.avg_price)
            return True
        await self._emergency_close(reason)
        return True

    async def _live_entry_guard_ok(self, allow_existing_position: bool = False) -> bool:
        if self.settings.is_dry_run:
            return True
        try:
            open_orders = await self.binance.open_orders()
            position_qty = await self.binance.position_quantity()
        except Exception as exc:  # noqa: BLE001
            self.state = StrategyState.PAUSED
            self.last_error = f"开仓前检查币安挂单/持仓失败，已暂停：{str(exc)[:120]}"
            self.storage.record_event("live_entry_guard_failed", {"error": str(exc)[:160]})
            return False
        mt4_positions = self.mt4.positions()
        mt4_account = self.mt4.account_snapshot()
        mt4_used_margin = mt4_account.used_margin if mt4_account else None
        if not allow_existing_position and (mt4_positions or (mt4_used_margin is not None and mt4_used_margin != 0)):
            self.state = StrategyState.PAUSED
            self.last_error = "开仓前发现 MT4 已有持仓或保证金占用，已暂停自动开仓，请先人工确认"
            self.storage.record_event(
                "live_entry_guard_blocked_mt4_position",
                {
                    "positions": [
                        {
                            "ticket": position.ticket,
                            "symbol": position.symbol,
                            "side": position.side.value,
                            "lots": str(position.lots),
                        }
                        for position in mt4_positions
                    ],
                    "used_margin": str(mt4_used_margin) if mt4_used_margin is not None else None,
                },
            )
            return False
        if allow_existing_position and not self._mt4_positions_match_open_pair(mt4_positions):
            self.state = StrategyState.PAUSED
            self.last_error = "补仓前发现 MT4 持仓方向和当前组合不一致，已暂停"
            self.storage.record_event(
                "add_guard_blocked_mt4_mismatch",
                {
                    "positions": [
                        {"ticket": position.ticket, "side": position.side.value, "lots": str(position.lots)}
                        for position in mt4_positions
                    ],
                },
            )
            return False
        arb_orders = [
            order
            for order in open_orders
            if order.client_order_id.startswith("arb_") and not order.reduce_only
        ]
        if arb_orders:
            self.state = StrategyState.PAUSED
            self.last_error = "开仓前发现币安遗留程序挂单，已暂停自动开仓"
            self.storage.record_event(
                "live_entry_guard_blocked",
                {
                    "position_qty": str(position_qty),
                    "open_orders": [
                        {
                            "order_id": order.order_id,
                            "side": order.side.value,
                            "price": str(order.price),
                            "executed_qty": str(order.executed_qty),
                        }
                        for order in arb_orders
                    ],
                },
            )
            return False
        if position_qty != 0 and not allow_existing_position:
            self.state = StrategyState.PAUSED
            self.last_error = "开仓前发现币安已有黄金持仓，已暂停自动开仓"
            self.storage.record_event("live_entry_guard_blocked_position", {"position_qty": str(position_qty)})
            return False
        if allow_existing_position and not self._binance_position_matches_open_pair(position_qty):
            self.state = StrategyState.PAUSED
            self.last_error = "补仓前发现币安持仓方向和当前组合不一致，已暂停"
            self.storage.record_event("add_guard_blocked_binance_mismatch", {"position_qty": str(position_qty)})
            return False
        return True

    def _mt4_positions_match_open_pair(self, positions) -> bool:
        if not self.open_pair:
            return False
        expected = Side.BUY if self.open_pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG else Side.SELL
        return bool(positions) and all(position.symbol == self.settings.mt4_symbol and position.side == expected for position in positions)

    def _binance_position_matches_open_pair(self, position_qty: Decimal) -> bool:
        if not self.open_pair or position_qty == 0:
            return False
        if self.open_pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
            return position_qty < 0
        return position_qty > 0

    async def _queue_mt4_hedge(self, qty: Decimal, fill_price: Decimal) -> None:
        if not self.active_plan or qty <= 0:
            return
        mt4_quote = self.mt4.latest_quote()
        if not mt4_quote:
            await self._emergency_close("MT4 quote missing before hedge")
            return
        if self.settings.is_dry_run:
            fill = mt4_quote.ask if self.active_plan.mt4_hedge_side == Side.BUY else mt4_quote.bid
            hedge_side = self.active_plan.mt4_hedge_side
            self.hedged_qty += qty
            self._mark_pair_open(fill, None)
            self.storage.record_event(
                "paper_mt4_hedge",
                {"side": hedge_side.value, "quantity": str(qty), "fill_price": str(fill)},
            )
            return
        lots = qty / self.settings.mt4_lot_size_oz
        self.mt4.queue_market_order(self.active_plan.mt4_hedge_side, lots, "entry hedge")
        self.hedge_started_ms = utc_now_ms()
        self.pending_hedge_qty += qty
        self.state = StrategyState.HEDGING_MT4

    async def _check_hedge_timeout(self) -> None:
        if self.hedge_started_ms <= 0:
            return
        if utc_now_ms() - self.hedge_started_ms > self.settings.max_hedge_delay_ms:
            await self._emergency_close("MT4 hedge timeout")

    async def _handle_mt4_reports(self) -> None:
        for report in self.mt4.drain_reports():
            self.storage.record_event("mt4_report", report.model_dump(mode="json"))
            if self.state not in {StrategyState.HEDGING_MT4, StrategyState.CLOSING_MT4}:
                if report.status == "ok" and report.ticket is not None:
                    self.last_error = "MT4 回报晚于当前策略状态，可能存在 MT4 单腿持仓，请人工确认"
                continue
            if report.status != "ok" or report.fill_price is None:
                if self.state == StrategyState.CLOSING_MT4:
                    ticket = report.ticket or self.pending_close_commands.get(report.command_id)
                    if ticket is not None:
                        self.pending_close_tickets.discard(ticket)
                    self.pending_close_commands.pop(report.command_id, None)
                    self.state = StrategyState.PAUSED
                    self.last_error = f"MT4 平仓失败，币安侧可能已平，需人工确认 MT4 剩余持仓：{report.message or report.error_code or '未知错误'}"
                    self.storage.record_event(
                        "mt4_close_failed",
                        {
                            "command_id": report.command_id,
                            "ticket": ticket,
                            "message": report.message,
                            "error_code": report.error_code,
                        },
                    )
                    continue
                await self._emergency_close(report.message or "MT4 command failed")
                continue
            qty = report.lots * self.settings.mt4_lot_size_oz
            if self.state == StrategyState.HEDGING_MT4:
                self.hedged_qty += qty
                self.pending_hedge_qty = max(Decimal("0"), self.pending_hedge_qty - qty)
                if self.pending_hedge_qty == 0:
                    self.hedge_started_ms = 0
                if self.active_order and self.hedged_qty >= self.active_order.executed_qty:
                    self._mark_pair_open(report.fill_price, report.ticket)
            elif self.state == StrategyState.CLOSING_MT4 and self.open_pair:
                ticket = report.ticket or self.pending_close_commands.get(report.command_id)
                self.pending_close_commands.pop(report.command_id, None)
                if ticket is None:
                    self.state = StrategyState.PAUSED
                    self.last_error = "MT4 平仓回报缺少单号，不能确认全部平仓，已暂停"
                    self.storage.record_event("mt4_close_ticket_unknown", report.model_dump(mode="json"))
                    continue
                self.pending_close_tickets.discard(ticket)
                if not self.pending_close_tickets:
                    self.storage.record_pnl(self.open_pair.pair_id, self.open_pair.realized_pnl)
                    self._reset_all()

    def _mark_pair_open(self, mt4_fill_price: Decimal, ticket: int | None) -> None:
        if not self.active_plan or not self.active_order:
            return
        active_qty = self.hedged_qty
        if self.adding_to_pair and self.open_pair:
            old_qty = self.open_pair.quantity_oz
            new_qty = old_qty + active_qty
            binance_entry_price = ((self.open_pair.binance_entry_price * old_qty) + (self.active_order.avg_price * active_qty)) / new_qty
            mt4_entry_price = ((self.open_pair.mt4_entry_price * old_qty) + (mt4_fill_price * active_qty)) / new_qty
            add_edge = self._entry_spread(self.open_pair.direction, self.active_order.avg_price, mt4_fill_price)
            tickets = list(self.open_pair.mt4_tickets or ([] if self.open_pair.mt4_ticket is None else [self.open_pair.mt4_ticket]))
            if ticket is not None:
                tickets.append(ticket)
            self.open_pair = self.open_pair.model_copy(
                update={
                    "quantity_oz": new_qty,
                    "binance_entry_price": binance_entry_price,
                    "mt4_entry_price": mt4_entry_price,
                    "binance_order_id": self.active_order.order_id,
                    "mt4_ticket": tickets[0] if tickets else None,
                    "mt4_tickets": tickets,
                    "add_count": self.open_pair.add_count + 1,
                    "last_add_edge": add_edge,
                }
            )
        else:
            tickets = [] if ticket is None else [ticket]
            entry_edge = self._entry_spread(self.active_plan.direction, self.active_order.avg_price, mt4_fill_price)
            self.open_pair = OpenPair(
                direction=self.active_plan.direction,
                quantity_oz=active_qty,
                binance_entry_price=self.active_order.avg_price,
                mt4_entry_price=mt4_fill_price,
                binance_order_id=self.active_order.order_id,
                mt4_ticket=ticket,
                mt4_tickets=tickets,
                base_edge=entry_edge,
                last_add_edge=entry_edge,
            )
        self.active_plan = None
        self.active_order = None
        self.adding_to_pair = False
        self.hedged_qty = Decimal("0")
        self.pending_hedge_qty = Decimal("0")
        self.hedge_started_ms = 0
        self._close_trigger_cache = None
        self._close_trigger_cache_ms = 0
        self.state = StrategyState.PAIR_OPEN

    async def _maybe_add_position(self, binance_quote: MarketQuote | None, mt4_quote: MarketQuote | None) -> bool:
        if not self.open_pair or not binance_quote or not mt4_quote:
            return False
        if self.settings.max_add_count <= 0 or self.open_pair.add_count >= self.settings.max_add_count:
            return False
        base_edge = self._last_add_edge()
        if base_edge is None:
            return False
        trigger_edge = base_edge + self.settings.add_edge_growth_usd
        plan = build_directional_entry_plan(
            self.settings,
            self.binance.filters,
            binance_quote,
            mt4_quote,
            self.open_pair.direction,
            trigger_edge,
        )
        if not plan:
            return False
        if not await self._live_entry_guard_ok(allow_existing_position=True):
            return True
        order = await self.binance.place_post_only_order(
            OrderRequest(
                symbol=self.settings.binance_symbol,
                side=plan.binance_side,
                quantity=plan.quantity_oz,
                price=plan.limit_price,
                post_only=True,
            )
        )
        self.storage.record_event(
            "add_order",
            {
                **order.model_dump(mode="json"),
                "trigger_edge": str(trigger_edge),
                "current_edge": str(plan.edge),
                "add_count": self.open_pair.add_count + 1,
            },
        )
        if order.status == OrderStatus.REJECTED:
            self.last_entry_cancel_ms = utc_now_ms()
            return True
        self.active_plan = plan
        self.active_order = order
        self.order_created_ms = utc_now_ms()
        self.hedged_qty = Decimal("0")
        self.pending_hedge_qty = Decimal("0")
        self.adding_to_pair = True
        self.state = StrategyState.QUOTING_BINANCE_ENTRY
        return True

    def _last_add_edge(self) -> Decimal | None:
        if not self.open_pair:
            return None
        if self.open_pair.add_count == 0:
            actual = self._current_pair_entry_spread()
            if actual is not None:
                return actual
        return self.open_pair.last_add_edge or self.open_pair.base_edge

    def _entry_spread(self, direction: PairDirection, binance_entry: Decimal, mt4_entry: Decimal) -> Decimal:
        if direction == PairDirection.BINANCE_SHORT_MT4_LONG:
            return binance_entry - mt4_entry
        return mt4_entry - binance_entry

    def _current_pair_entry_spread(self) -> Decimal | None:
        if not self.open_pair:
            return None
        mt4_entry = self._mt4_average_entry_price() or self.open_pair.mt4_entry_price
        return self._entry_spread(self.open_pair.direction, self.open_pair.binance_entry_price, mt4_entry)

    def _current_edge(self, direction: PairDirection, binance_quote: MarketQuote, mt4_quote: MarketQuote) -> Decimal:
        if direction == PairDirection.BINANCE_SHORT_MT4_LONG:
            return binance_quote.ask - mt4_quote.ask
        return mt4_quote.bid - binance_quote.bid

    async def _maybe_exit(
        self,
        binance_quote: MarketQuote | None,
        mt4_quote: MarketQuote | None,
        force: bool = False,
        reason: str | None = None,
    ) -> None:
        if not self.open_pair or not binance_quote or not mt4_quote:
            return
        if not force and not await self._close_spread_ready(binance_quote, mt4_quote):
            return
        current_spread = self._current_exit_spread(binance_quote, mt4_quote)
        break_even_spread = await self._break_even_spread()
        trigger_spread = await self._close_trigger_spread()
        if self.open_pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
            side = Side.BUY
            price = round_down(binance_quote.bid, self.binance.filters.tick_size)
            position_side = "SHORT"
        else:
            side = Side.SELL
            price = round_up(binance_quote.ask, self.binance.filters.tick_size)
            position_side = "LONG"
        order = await self.binance.place_post_only_order(
            OrderRequest(
                symbol=self.settings.binance_symbol,
                side=side,
                quantity=self.open_pair.quantity_oz,
                price=price,
                post_only=True,
                reduce_only=True,
                position_side=position_side,
            )
        )
        if order.status != OrderStatus.REJECTED:
            self.storage.record_event(
                "exit_order",
                {
                    **order.model_dump(mode="json"),
                    "force": force,
                    "reason": reason,
                    "current_spread": str(current_spread) if current_spread is not None else None,
                    "break_even_spread": str(break_even_spread) if break_even_spread is not None else None,
                    "trigger_spread": str(trigger_spread) if trigger_spread is not None else None,
                    "exit_follow_buffer": str(self._exit_follow_buffer_usd_per_oz()),
                    "close_profit_usd_per_oz": str(self._effective_close_profit_usd_per_oz()),
                },
            )
            self.active_order = order
            self.order_created_ms = utc_now_ms()
            self.exit_force_reason = reason if force else None
            if force:
                self.storage.record_event(
                    "forced_exit_order",
                    {
                        **order.model_dump(mode="json"),
                        "reason": reason,
                        "mt4_swap_estimate": str(self._mt4_swap_estimate()) if self._mt4_swap_estimate() is not None else None,
                    },
                )
            self.state = StrategyState.QUOTING_BINANCE_EXIT

    async def _check_exit_order(self) -> None:
        if not self.active_order or not self.open_pair:
            self.state = StrategyState.PAIR_OPEN
            return
        if utc_now_ms() - self.order_created_ms > self.settings.max_order_age_ms:
            order = await self._cancel_or_refresh_expired_exit_order(self.active_order)
            if not order or order.status != OrderStatus.FILLED:
                self.active_order = None
                self.exit_force_reason = None
                self.state = StrategyState.PAIR_OPEN
                return
        else:
            order = await self.binance.get_order(self.active_order.order_id)
        if not order or order.status != OrderStatus.FILLED:
            if order and order.status == OrderStatus.NEW and not self.exit_force_reason and not await self._exit_spread_still_valid():
                await self.binance.cancel_order(order.order_id)
                self.active_order = None
                self.state = StrategyState.PAIR_OPEN
                self.storage.record_event(
                    "exit_order_canceled_spread_widened",
                    {"order_id": order.order_id, "reason": "平仓挂单等待期间价差走扩"},
                )
            return
        self.active_order = order
        binance_quote = self.binance.latest_quote()
        mt4_quote = self.mt4.latest_quote()
        self.storage.record_event(
            "exit_order_filled",
            {
                **order.model_dump(mode="json"),
                "current_spread": str(self._current_exit_spread(binance_quote, mt4_quote)) if binance_quote and mt4_quote else None,
                "mt4_bid": str(mt4_quote.bid) if mt4_quote else None,
                "mt4_ask": str(mt4_quote.ask) if mt4_quote else None,
            },
        )
        if self.settings.is_dry_run:
            self.storage.record_pnl(self.open_pair.pair_id, self.open_pair.realized_pnl)
            self._reset_all()
            return
        tickets = list(self.open_pair.mt4_tickets or ([] if self.open_pair.mt4_ticket is None else [self.open_pair.mt4_ticket]))
        if not tickets:
            self.state = StrategyState.PAUSED
            self.last_error = "MT4 持仓单号缺失，不能自动平仓，已暂停"
            self.storage.record_event(
                "mt4_close_ticket_missing",
                {"pair_id": self.open_pair.pair_id, "quantity_oz": str(self.open_pair.quantity_oz)},
            )
            return
        lots_by_ticket = self._mt4_close_lots_by_ticket(tickets)
        self.pending_close_tickets = set(tickets)
        self.pending_close_commands = {}
        for ticket in tickets:
            command = self.mt4.queue_close(ticket, lots_by_ticket[ticket], "exit hedge")
            self.pending_close_commands[command.command_id] = ticket
        self.storage.record_event(
            "mt4_close_queued",
            {
                "pair_id": self.open_pair.pair_id,
                "tickets": tickets,
                "lots_by_ticket": {str(ticket): str(lots) for ticket, lots in lots_by_ticket.items()},
            },
        )
        self.state = StrategyState.CLOSING_MT4

    async def _cancel_or_refresh_expired_exit_order(self, order: OrderUpdate) -> OrderUpdate | None:
        if order.status in TERMINAL_ORDER_STATUSES:
            return order
        try:
            canceled = await self.binance.cancel_order(order.order_id)
            if canceled:
                return canceled
        except BinanceError as exc:
            if not _binance_order_missing(exc):
                raise
            self.storage.record_event("exit_order_cancel_not_visible", {"order_id": order.order_id, "error": str(exc)[:160]})
        try:
            refreshed = await self.binance.get_order(order.order_id)
            if refreshed:
                return refreshed
        except BinanceError as exc:
            if not _binance_order_missing(exc):
                raise
            self.storage.record_event("exit_order_missing_after_cancel", {"order_id": order.order_id, "error": str(exc)[:160]})
        return None

    def _negative_swap_exit_reason(self) -> str | None:
        if not self.open_pair or self.settings.negative_swap_close_before_minutes <= 0:
            return None
        swap_info = self.mt4.latest_swap_info()
        next_rollover = swap_info.next_rollover_time_ms
        if next_rollover is None:
            return None
        ms_left = next_rollover - utc_now_ms()
        lead_ms = self.settings.negative_swap_close_before_minutes * 60 * 1000
        if ms_left < 0 or ms_left > lead_ms:
            return None
        estimate = self._mt4_swap_estimate()
        if estimate is None or estimate >= 0:
            return None
        projected_net = self._convergence_net_after_next_mt4_swap(estimate)
        if projected_net is not None and projected_net > 0:
            self.storage.record_event(
                "negative_swap_hold_allowed",
                {
                    "mt4_swap_estimate": str(estimate),
                    "projected_convergence_net": str(projected_net),
                    "next_rollover_time_ms": next_rollover,
                },
            )
            return None
        minutes_left = max(0, ms_left // 60000)
        net_text = f"，扣后回归净利预估 {projected_net}" if projected_net is not None else ""
        return f"MT4 隔夜费预估为亏损 {estimate}{net_text}，距离结算约 {minutes_left} 分钟，提前平仓"

    def _mt4_swap_estimate(self) -> Decimal | None:
        if not self.open_pair:
            return None
        swap_info = self.mt4.latest_swap_info()
        raw = (
            swap_info.swap_long_per_lot
            if self.open_pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG
            else swap_info.swap_short_per_lot
        )
        if raw is None:
            return None
        lots = self.open_pair.quantity_oz / self.settings.mt4_lot_size_oz
        if swap_info.swap_type == 0:
            if not swap_info.tick_value or not swap_info.tick_size or not swap_info.point:
                return raw * lots
            return raw * (swap_info.point / swap_info.tick_size) * swap_info.tick_value * lots
        return raw * lots

    def _convergence_net_after_next_mt4_swap(self, next_swap: Decimal) -> Decimal | None:
        if not self.open_pair:
            return None
        qty = self.open_pair.quantity_oz
        if self.open_pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
            opening_edge = self.open_pair.binance_entry_price - self.open_pair.mt4_entry_price
        else:
            opening_edge = self.open_pair.mt4_entry_price - self.open_pair.binance_entry_price
        gross = (opening_edge - self.settings.close_max_spread) * qty
        fee_rate = self.binance.maker_fee_rate or self.settings.binance_maker_fee_rate
        if fee_rate is not None:
            gross -= self.open_pair.binance_entry_price * qty * abs(fee_rate) * Decimal("2")
        accrued_swap = self._mt4_accrued_swap()
        if accrued_swap is not None:
            gross += accrued_swap
        return gross + next_swap

    def _mt4_accrued_swap(self) -> Decimal | None:
        if not self.open_pair:
            return None
        positions = self.mt4.positions()
        if not positions:
            return None
        tickets = set(self.open_pair.mt4_tickets or ([] if self.open_pair.mt4_ticket is None else [self.open_pair.mt4_ticket]))
        if tickets:
            matched = [position for position in positions if position.ticket in tickets]
        else:
            expected_side = Side.BUY if self.open_pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG else Side.SELL
            matched = [position for position in positions if position.symbol == self.settings.mt4_symbol and position.side == expected_side]
        if not matched:
            return None
        return sum((position.swap for position in matched), Decimal("0"))

    def _mt4_close_lots_by_ticket(self, tickets: list[int]) -> dict[int, Decimal]:
        positions_by_ticket = {
            position.ticket: position.lots
            for position in self.mt4.positions()
            if position.ticket in tickets and position.symbol == self.settings.mt4_symbol
        }
        if len(positions_by_ticket) == len(tickets):
            return positions_by_ticket
        fallback_lots = (self.open_pair.quantity_oz / self.settings.mt4_lot_size_oz / Decimal(len(tickets))) if self.open_pair else Decimal("0")
        return {ticket: positions_by_ticket.get(ticket, fallback_lots) for ticket in tickets}

    async def _exit_spread_still_valid(self) -> bool:
        binance_quote = self.binance.latest_quote()
        mt4_quote = self.mt4.latest_quote()
        if not binance_quote or not mt4_quote:
            return False
        return await self._close_spread_ready(binance_quote, mt4_quote)

    async def _close_spread_ready(self, binance_quote: MarketQuote, mt4_quote: MarketQuote) -> bool:
        current = self._current_exit_spread(binance_quote, mt4_quote)
        trigger = await self._close_trigger_spread()
        if current is None or trigger is None:
            return False
        return current <= trigger

    def _current_exit_spread(self, binance_quote: MarketQuote, mt4_quote: MarketQuote) -> Decimal | None:
        if not self.open_pair:
            return None
        if self.open_pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
            return round_down(binance_quote.bid, self.binance.filters.tick_size) - mt4_quote.bid
        return mt4_quote.ask - round_up(binance_quote.ask, self.binance.filters.tick_size)

    async def _close_trigger_spread(self) -> Decimal | None:
        if not self.open_pair:
            return None
        now = utc_now_ms()
        if self._close_trigger_cache is not None and now - self._close_trigger_cache_ms <= 2000:
            return self._close_trigger_cache
        break_even = await self._break_even_spread()
        if break_even is None:
            trigger = None
        else:
            trigger = break_even - self._effective_close_profit_usd_per_oz() - self._exit_follow_buffer_usd_per_oz()
        self._close_trigger_cache = trigger
        self._close_trigger_cache_ms = now
        return trigger

    async def _break_even_spread(self) -> Decimal | None:
        if not self.open_pair or self.open_pair.quantity_oz <= 0:
            return None
        binance_entry = self.open_pair.binance_entry_price
        try:
            snapshot = await self.binance.position_snapshot()
            if snapshot and snapshot.entry_price is not None and snapshot.position_amt != 0:
                binance_entry = snapshot.entry_price
        except Exception as exc:  # noqa: BLE001
            self.storage.record_event("close_binance_entry_snapshot_failed", {"error": str(exc)[:160]})
        mt4_entry = self._mt4_average_entry_price() or self.open_pair.mt4_entry_price
        if self.open_pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
            entry_spread = binance_entry - mt4_entry
        else:
            entry_spread = mt4_entry - binance_entry
        funding = await self._binance_funding_income_since_open()
        accrued_swap = self._mt4_accrued_swap() or Decimal("0")
        estimated_fees = self._estimated_round_trip_fees(binance_entry)
        return entry_spread + ((funding + accrued_swap - estimated_fees) / self.open_pair.quantity_oz)

    def _exit_follow_buffer_usd_per_oz(self) -> Decimal:
        point = self.mt4.latest_swap_info().point or Decimal("0.01")
        return Decimal(self.settings.mt4_slippage_points) * point

    def _effective_close_profit_usd_per_oz(self) -> Decimal:
        if not self.open_pair or self.settings.max_pair_age_minutes <= 0:
            return self.settings.close_profit_usd_per_oz
        age_ms = utc_now_ms() - self.open_pair.opened_ms
        if age_ms >= self.settings.max_pair_age_minutes * 60_000:
            return min(self.settings.close_profit_usd_per_oz, self.settings.aged_close_profit_usd_per_oz)
        return self.settings.close_profit_usd_per_oz

    async def _binance_funding_income_since_open(self) -> Decimal:
        if not self.open_pair or self.settings.is_dry_run:
            return Decimal("0")
        try:
            rows = await self.binance.funding_income(self.open_pair.opened_ms - 60_000, utc_now_ms(), limit=1000)
        except Exception as exc:  # noqa: BLE001
            self.storage.record_event("close_funding_income_failed", {"error": str(exc)[:160]})
            return Decimal("0")
        total = Decimal("0")
        for row in rows:
            value = row.get("income")
            if value is not None:
                total += Decimal(str(value))
        return total

    def _estimated_round_trip_fees(self, binance_entry: Decimal) -> Decimal:
        if not self.open_pair:
            return Decimal("0")
        fee_rate = self.binance.maker_fee_rate or self.settings.binance_maker_fee_rate or Decimal("0")
        quote = self.binance.latest_quote()
        if self.open_pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG and quote:
            exit_price = round_down(quote.bid, self.binance.filters.tick_size)
        elif quote:
            exit_price = round_up(quote.ask, self.binance.filters.tick_size)
        else:
            exit_price = binance_entry
        return (binance_entry + exit_price) * self.open_pair.quantity_oz * abs(fee_rate)

    def _mt4_average_entry_price(self) -> Decimal | None:
        if not self.open_pair:
            return None
        positions = self.mt4.positions()
        tickets = set(self.open_pair.mt4_tickets or ([] if self.open_pair.mt4_ticket is None else [self.open_pair.mt4_ticket]))
        if tickets:
            matched = [position for position in positions if position.ticket in tickets]
        else:
            expected_side = Side.BUY if self.open_pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG else Side.SELL
            matched = [position for position in positions if position.symbol == self.settings.mt4_symbol and position.side == expected_side]
        total_lots = sum((position.lots for position in matched), Decimal("0"))
        if total_lots <= 0:
            return None
        return sum((position.open_price * position.lots for position in matched), Decimal("0")) / total_lots

    async def _emergency_close(self, reason: str) -> None:
        self.last_error = reason
        self.state = StrategyState.EMERGENCY_CLOSE_BINANCE
        close_order: OrderUpdate | None = None
        if self.active_order and self.active_order.executed_qty > 0:
            side = Side.BUY if self.active_order.side == Side.SELL else Side.SELL
            close_order = await self.binance.place_market_order(
                OrderRequest(
                    symbol=self.settings.binance_symbol,
                    side=side,
                    quantity=self.active_order.executed_qty,
                    post_only=False,
                    reduce_only=True,
                )
            )
        self.storage.record_event(
            "emergency_close",
            {
                "reason": reason,
                "close_order_id": close_order.order_id if close_order else None,
                "close_status": close_order.status.value if close_order else None,
            },
        )
        self.active_plan = None
        self.active_order = None
        self.adding_to_pair = False
        self.exit_force_reason = None
        self.pending_hedge_qty = Decimal("0")
        self.hedge_started_ms = 0
        self.state = StrategyState.PAUSED

    def _quotes_fresh(self, binance_quote: MarketQuote | None, mt4_quote: MarketQuote | None) -> bool:
        for quote in (binance_quote, mt4_quote):
            check = self.risk.quote_fresh(quote)
            if not check.ok:
                self.last_error = check.reason
                return False
        self.last_error = None
        return True

    def _clear_entry(self) -> None:
        return_to_pair = self.adding_to_pair and self.open_pair is not None
        self.last_entry_cancel_ms = utc_now_ms()
        self._clear_entry_candidate()
        self.active_plan = None
        self.active_order = None
        self.adding_to_pair = False
        self.exit_force_reason = None
        self.hedged_qty = Decimal("0")
        self.pending_hedge_qty = Decimal("0")
        self.hedge_started_ms = 0
        self._close_trigger_cache = None
        self._close_trigger_cache_ms = 0
        self.state = StrategyState.PAIR_OPEN if return_to_pair else StrategyState.IDLE

    def _reset_all(self) -> None:
        self._clear_entry_candidate()
        self.active_plan = None
        self.active_order = None
        self.open_pair = None
        self.adding_to_pair = False
        self.exit_force_reason = None
        self.pending_close_tickets = set()
        self.pending_close_commands = {}
        self.hedged_qty = Decimal("0")
        self.pending_hedge_qty = Decimal("0")
        self.hedge_started_ms = 0
        self._close_trigger_cache = None
        self._close_trigger_cache_ms = 0
        self.state = StrategyState.IDLE
