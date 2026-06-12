from __future__ import annotations

import logging
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from app.binance_client import BinanceBaseClient
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
        price = round_up(max(binance.ask, mt4.ask + settings.open_min_edge), filters.tick_size)
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
        price = round_down(min(binance.bid, mt4.bid - settings.open_min_edge), filters.tick_size)
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
            if not self._quotes_fresh(binance_quote, mt4_quote):
                return
            await self._maybe_enter(binance_quote, mt4_quote)
        elif self.state == StrategyState.QUOTING_BINANCE_ENTRY:
            if not self._quotes_fresh(binance_quote, mt4_quote):
                if self.active_order:
                    await self.binance.cancel_order(self.active_order.order_id)
                self.state = StrategyState.PAUSED
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

    async def _maybe_enter(self, binance_quote: MarketQuote | None, mt4_quote: MarketQuote | None) -> None:
        if not binance_quote or not mt4_quote:
            return
        plan = build_entry_plan(self.settings, self.binance.filters, binance_quote, mt4_quote)
        if not plan:
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
        if order.status == OrderStatus.REJECTED:
            return
        self.active_plan = plan
        self.active_order = order
        self.order_created_ms = utc_now_ms()
        self.state = StrategyState.QUOTING_BINANCE_ENTRY

    async def _check_entry_order(self) -> None:
        if not self.active_order or not self.active_plan:
            self.state = StrategyState.IDLE
            return
        if utc_now_ms() - self.order_created_ms > self.settings.max_order_age_ms:
            await self.binance.cancel_order(self.active_order.order_id)
            self._clear_entry()
            return
        order = await self.binance.get_order(self.active_order.order_id)
        if not order:
            return
        self.active_order = order
        if order.status == OrderStatus.REJECTED:
            self._clear_entry()
            return
        if order.status == OrderStatus.PARTIALLY_FILLED and self.active_plan:
            current_plan = None
            binance_quote = self.binance.latest_quote()
            mt4_quote = self.mt4.latest_quote()
            if binance_quote and mt4_quote:
                current_plan = build_entry_plan(self.settings, self.binance.filters, binance_quote, mt4_quote)
            if not current_plan or current_plan.direction != self.active_plan.direction:
                await self.binance.cancel_order(order.order_id)
        if order.executed_qty > self.hedged_qty + self.pending_hedge_qty:
            await self._queue_mt4_hedge(order.executed_qty - self.hedged_qty - self.pending_hedge_qty, order.avg_price)
        if order.status == OrderStatus.CANCELED and order.executed_qty == self.hedged_qty:
            self._clear_entry()

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
        self.active_plan = None
        self.active_order = None
        self.hedged_qty = Decimal("0")
        self.pending_hedge_qty = Decimal("0")
        self.state = StrategyState.IDLE

    def _reset_all(self) -> None:
        self.active_plan = None
        self.active_order = None
        self.open_pair = None
        self.hedged_qty = Decimal("0")
        self.pending_hedge_qty = Decimal("0")
        self.state = StrategyState.IDLE
