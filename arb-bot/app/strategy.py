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
        self.pending_hedge_qty = Decimal("0")
        self.hedged_qty = Decimal("0")
        self.open_pair: OpenPair | None = None
        self.last_error: str | None = None

    async def step(self) -> None:
        await self._handle_mt4_reports()
        if self.state == StrategyState.PAUSED:
            return
        binance_quote = self.binance.latest_quote()
        mt4_quote = self.mt4.latest_quote()
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
                self.state = StrategyState.PAUSED
                return
            await self._maybe_exit(binance_quote, mt4_quote)
        elif self.state == StrategyState.QUOTING_BINANCE_EXIT:
            await self._check_exit_order()

    def resume(self) -> None:
        if self.state == StrategyState.PAUSED:
            self.state = StrategyState.IDLE
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

    async def _live_entry_guard_ok(self) -> bool:
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
        arb_orders = [
            order
            for order in open_orders
            if order.client_order_id.startswith("arb_") and not order.reduce_only
        ]
        if position_qty != 0 or arb_orders:
            self.state = StrategyState.PAUSED
            self.last_error = "开仓前发现币安已有黄金持仓或遗留程序挂单，已暂停自动开仓"
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
        return True

    async def _queue_mt4_hedge(self, qty: Decimal, fill_price: Decimal) -> None:
        if not self.active_plan or qty <= 0:
            return
        mt4_quote = self.mt4.latest_quote()
        if not mt4_quote:
            await self._emergency_close("MT4 quote missing before hedge")
            return
        if self.active_plan.mt4_hedge_side == Side.BUY:
            check = self.risk.mt4_buy_price_ok(fill_price, mt4_quote.ask)
            max_price = fill_price - self.settings.min_locked_edge
            min_price = None
        else:
            check = self.risk.mt4_sell_price_ok(fill_price, mt4_quote.bid)
            max_price = None
            min_price = fill_price + self.settings.min_locked_edge
        if not check.ok:
            await self._emergency_close(check.reason)
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
        self.mt4.queue_market_order(self.active_plan.mt4_hedge_side, lots, "entry hedge", max_price, min_price)
        self.pending_hedge_qty += qty
        self.state = StrategyState.HEDGING_MT4

    async def _check_hedge_timeout(self) -> None:
        if utc_now_ms() - self.order_created_ms > self.settings.max_hedge_delay_ms:
            await self._emergency_close("MT4 hedge timeout")

    async def _handle_mt4_reports(self) -> None:
        for report in self.mt4.drain_reports():
            self.storage.record_event("mt4_report", report.model_dump(mode="json"))
            if self.state not in {StrategyState.HEDGING_MT4, StrategyState.CLOSING_MT4}:
                continue
            if report.status != "ok" or report.fill_price is None:
                await self._emergency_close(report.message or "MT4 command failed")
                continue
            qty = report.lots * self.settings.mt4_lot_size_oz
            if self.state == StrategyState.HEDGING_MT4:
                self.hedged_qty += qty
                self.pending_hedge_qty = max(Decimal("0"), self.pending_hedge_qty - qty)
                if self.active_order and self.hedged_qty >= self.active_order.executed_qty:
                    self._mark_pair_open(report.fill_price, report.ticket)
            elif self.state == StrategyState.CLOSING_MT4 and self.open_pair:
                self.storage.record_pnl(self.open_pair.pair_id, self.open_pair.realized_pnl)
                self._reset_all()

    def _mark_pair_open(self, mt4_fill_price: Decimal, ticket: int | None) -> None:
        if not self.active_plan or not self.active_order:
            return
        self.open_pair = OpenPair(
            direction=self.active_plan.direction,
            quantity_oz=self.hedged_qty,
            binance_entry_price=self.active_order.avg_price,
            mt4_entry_price=mt4_fill_price,
            binance_order_id=self.active_order.order_id,
            mt4_ticket=ticket,
        )
        self.active_plan = None
        self.active_order = None
        self.pending_hedge_qty = Decimal("0")
        self.state = StrategyState.PAIR_OPEN

    async def _maybe_exit(self, binance_quote: MarketQuote | None, mt4_quote: MarketQuote | None) -> None:
        if not self.open_pair or not binance_quote or not mt4_quote:
            return
        if abs(binance_quote.mid - mt4_quote.mid) > self.settings.close_max_spread:
            return
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
            self.active_order = order
            self.order_created_ms = utc_now_ms()
            self.state = StrategyState.QUOTING_BINANCE_EXIT

    async def _check_exit_order(self) -> None:
        if not self.active_order or not self.open_pair:
            self.state = StrategyState.PAIR_OPEN
            return
        if utc_now_ms() - self.order_created_ms > self.settings.max_order_age_ms:
            await self.binance.cancel_order(self.active_order.order_id)
            self.state = StrategyState.PAIR_OPEN
            return
        order = await self.binance.get_order(self.active_order.order_id)
        if not order or order.status != OrderStatus.FILLED:
            return
        self.active_order = order
        if self.settings.is_dry_run:
            self.storage.record_pnl(self.open_pair.pair_id, self.open_pair.realized_pnl)
            self._reset_all()
            return
        close_side = Side.SELL if self.open_pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG else Side.BUY
        lots = self.open_pair.quantity_oz / self.settings.mt4_lot_size_oz
        self.mt4.queue_market_order(close_side, lots, "exit hedge")
        self.state = StrategyState.CLOSING_MT4

    async def _emergency_close(self, reason: str) -> None:
        self.last_error = reason
        self.state = StrategyState.EMERGENCY_CLOSE_BINANCE
        if self.active_order and self.active_order.executed_qty > 0:
            side = Side.BUY if self.active_order.side == Side.SELL else Side.SELL
            await self.binance.place_market_order(
                OrderRequest(
                    symbol=self.settings.binance_symbol,
                    side=side,
                    quantity=self.active_order.executed_qty,
                    post_only=False,
                    reduce_only=True,
                )
            )
        self.storage.record_event("emergency_close", {"reason": reason})
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
        self.last_entry_cancel_ms = utc_now_ms()
        self._clear_entry_candidate()
        self.active_plan = None
        self.active_order = None
        self.hedged_qty = Decimal("0")
        self.pending_hedge_qty = Decimal("0")
        self.state = StrategyState.IDLE

    def _reset_all(self) -> None:
        self._clear_entry_candidate()
        self.active_plan = None
        self.active_order = None
        self.open_pair = None
        self.hedged_qty = Decimal("0")
        self.pending_hedge_qty = Decimal("0")
        self.state = StrategyState.IDLE
