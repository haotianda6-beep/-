from __future__ import annotations

import asyncio
import json
import logging
import os
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse

from app.binance_client import BinanceError, BinanceFuturesClient, PaperBinanceClient
from app.config import Settings, existing_env_paths, load_settings, update_local_config_file, update_mode_file
from app.logger import setup_logging
from app.history import build_spread_analysis
from app.live_reconcile import open_pair_live_reconcile_action
from app.models import (
    BinancePositionSnapshot,
    EngineStatus,
    ExecutionPlanStatus,
    MarketQuote,
    Mt4ClosedOrder,
    Mt4HistoryPayload,
    Mt4OrderHistoryPayload,
    Mt4Report,
    Mt4Tick,
    OpenPair,
    OrderRequest,
    OrderStatus,
    PairDirection,
    PositionMetrics,
    RuntimeConfig,
    RuntimeConfigUpdate,
    Side,
    SpreadAnalysis,
    StrategyState,
    TradeHistoryItem,
    TradeHistoryResponse,
    utc_now_ms,
)
from app.mt4_bridge import Mt4Bridge
from app.risk import RiskManager
from app.storage import Storage
from app.strategy import (
    StrategyEngine,
    build_directional_entry_plan,
    build_entry_plan,
    round_down,
    round_up,
)


setup_logging()
logger = logging.getLogger(__name__)

settings: Settings = load_settings()
storage = Storage(settings.sqlite_path)
mt4_bridge = Mt4Bridge(settings)
binance_client = PaperBinanceClient(settings) if settings.is_dry_run else BinanceFuturesClient(settings)
risk = RiskManager(settings, storage)
strategy = StrategyEngine(settings, binance_client, mt4_bridge, risk, storage)

app = FastAPI(title="黄金价差执行器", version="0.1.0")
_loop_task: asyncio.Task | None = None
_binance_position_qty_cache: Decimal | None = None
_binance_position_qty_cache_ms = 0
_binance_position_snapshot_cache: BinancePositionSnapshot | None = None
_binance_position_snapshot_cache_ms = 0
_binance_accrued_funding_cache_pair_id: str | None = None
_binance_accrued_funding_cache_value: Decimal | None = None
_binance_accrued_funding_cache_ms = 0
_runtime_state_cache: str | None = None
_live_pair_reconcile_ms = 0
WEB_DIR = Path(__file__).resolve().parents[1] / "web"
MT4_DIR = Path(__file__).resolve().parents[1] / "mt4"
RUNTIME_STATE_PATH = settings.sqlite_path.parent / "runtime_state.json"


@app.on_event("startup")
async def startup() -> None:
    global _loop_task
    await binance_client.start()
    _load_runtime_state()
    await _reconcile_live_startup_state()
    mode = "dry-run" if settings.is_dry_run else "live"
    logger.info("arb executor starting mode=%s symbol=%s/%s", mode, settings.binance_symbol, settings.mt4_symbol)
    _loop_task = asyncio.create_task(_strategy_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    if _loop_task:
        _loop_task.cancel()
    await _cancel_unfilled_active_order_on_shutdown()
    await _cancel_orphan_arb_orders("shutdown")
    await binance_client.stop()


async def _cancel_unfilled_active_order_on_shutdown() -> None:
    order = strategy.active_order
    if settings.is_dry_run or order is None:
        return
    try:
        latest = await binance_client.get_order(order.order_id)
        if latest is not None:
            order = latest
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to refresh active Binance order during shutdown: %s", str(exc)[:160])
    if order.status not in {OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED, OrderStatus.FILLED}:
        try:
            canceled = await binance_client.cancel_order(order.order_id)
            if canceled is not None:
                order = canceled
            logger.info("canceled active Binance order during shutdown order_id=%s", order.order_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to cancel active Binance order during shutdown: %s", str(exc)[:160])
    if order.executed_qty > 0:
        unhedged_qty = order.executed_qty - strategy.hedged_qty - strategy.pending_hedge_qty
        logger.warning(
            "active Binance order has fills during shutdown order_id=%s executed_qty=%s unhedged_qty=%s",
            order.order_id,
            order.executed_qty,
            unhedged_qty,
        )
        if strategy.open_pair is None and unhedged_qty > 0:
            await _emergency_close_binance_fill(order, unhedged_qty, "服务关闭时发现币安已有未对冲成交")
        return
    if order.status in {OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED}:
        return


async def _reconcile_live_startup_state() -> None:
    if settings.is_dry_run:
        return
    await _cancel_orphan_arb_orders("startup")
    try:
        qty = await binance_client.position_quantity()
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to inspect Binance position on startup: %s", str(exc)[:160])
        strategy.state = StrategyState.PAUSED
        strategy.last_error = "启动时检查币安持仓失败，已暂停自动挂单"
        return
    if qty != 0 and strategy.open_pair is None:
        strategy.state = StrategyState.PAUSED
        strategy.last_error = f"启动时检测到币安已有 {settings.binance_symbol} 持仓 {qty}，已暂停自动挂单"
        storage.record_event(
            "startup_existing_binance_position",
            {"symbol": settings.binance_symbol, "position_qty": str(qty)},
        )


async def _cancel_orphan_arb_orders(reason: str) -> None:
    if settings.is_dry_run:
        return
    try:
        orders = await binance_client.open_orders()
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to inspect open Binance orders during %s: %s", reason, str(exc)[:160])
        strategy.state = StrategyState.PAUSED
        strategy.last_error = "检查币安遗留挂单失败，已暂停自动挂单"
        return
    for order in orders:
        if not order.client_order_id.startswith("arb_"):
            continue
        final_order = order
        try:
            if order.status not in {OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED, OrderStatus.FILLED}:
                canceled = await binance_client.cancel_order(order.order_id)
                if canceled is not None:
                    final_order = canceled
            storage.record_event(
                "orphan_binance_order_canceled",
                {
                    "reason": reason,
                    "order_id": final_order.order_id,
                    "client_order_id": final_order.client_order_id,
                    "side": final_order.side.value,
                    "status": final_order.status.value,
                    "executed_qty": str(final_order.executed_qty),
                    "reduce_only": final_order.reduce_only,
                },
            )
            if not final_order.reduce_only and final_order.executed_qty > 0:
                await _emergency_close_binance_fill(final_order, final_order.executed_qty, f"{reason} 时发现遗留开仓单已有成交")
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to cancel orphan Binance order during %s: %s", reason, str(exc)[:160])
            strategy.state = StrategyState.PAUSED
            strategy.last_error = "处理币安遗留挂单失败，已暂停自动挂单"


async def _emergency_close_binance_fill(order, quantity: Decimal, reason: str) -> None:
    if quantity <= 0:
        return
    close_side = Side.BUY if order.side == Side.SELL else Side.SELL
    close_order = await binance_client.place_market_order(
        OrderRequest(
            symbol=settings.binance_symbol,
            side=close_side,
            quantity=quantity,
            post_only=False,
            reduce_only=True,
        )
    )
    storage.record_event(
        "binance_emergency_close",
        {
            "reason": reason,
            "source_order_id": order.order_id,
            "close_order_id": close_order.order_id,
            "side": close_side.value,
            "quantity": str(quantity),
            "status": close_order.status.value,
            "avg_price": str(close_order.avg_price),
        },
    )
    logger.warning(
        "emergency closed unhedged Binance fill reason=%s source_order_id=%s quantity=%s close_order_id=%s",
        reason,
        order.order_id,
        quantity,
        close_order.order_id,
    )


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "state": strategy.state,
        "binance_quote": binance_client.latest_quote() is not None,
        "mt4_connected": mt4_bridge.connected(),
        "paper_mode": settings.paper_mode,
        "live_trading": settings.live_trading,
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/ea/ArbBridgeEA.mq4")
async def download_ea() -> FileResponse:
    return FileResponse(MT4_DIR / "ArbBridgeEA.mq4", filename="ArbBridgeEA.mq4", media_type="text/plain")


@app.get("/status", response_model=EngineStatus)
async def status() -> EngineStatus:
    metrics = await _position_metrics()
    return EngineStatus(
        state=strategy.state,
        live_trading=settings.live_trading,
        paper_mode=settings.paper_mode,
        binance_connected=binance_client.latest_quote() is not None,
        mt4_connected=mt4_bridge.connected(),
        binance_symbol=settings.binance_symbol,
        mt4_symbol=settings.mt4_symbol,
        maker_fee_rate=binance_client.maker_fee_rate,
        binance_funding=binance_client.latest_funding(),
        binance_account=await binance_client.account_snapshot(),
        mt4_account=mt4_bridge.account_snapshot(),
        binance_position_qty=await _binance_position_quantity(),
        mt4_positions=mt4_bridge.positions(),
        binance_quote=binance_client.latest_quote(),
        mt4_quote=mt4_bridge.latest_quote(),
        open_pair=strategy.open_pair,
        position_metrics=metrics,
        execution_plan=_execution_plan(metrics),
        last_error=strategy.last_error,
        config=_runtime_config(),
    )


async def _binance_position_quantity() -> Decimal | None:
    global _binance_position_qty_cache, _binance_position_qty_cache_ms
    snapshot = await _binance_position_snapshot()
    if snapshot is not None:
        return snapshot.position_amt
    now = _now_ms()
    if _binance_position_qty_cache is not None and now - _binance_position_qty_cache_ms <= 1500:
        return _binance_position_qty_cache
    try:
        _binance_position_qty_cache = await binance_client.position_quantity()
        _binance_position_qty_cache_ms = now
    except Exception as exc:  # noqa: BLE001
        logger.warning("Binance position quantity unavailable: %s", str(exc)[:160])
    return _binance_position_qty_cache


async def _binance_position_snapshot() -> BinancePositionSnapshot | None:
    global _binance_position_snapshot_cache, _binance_position_snapshot_cache_ms
    now = _now_ms()
    if _binance_position_snapshot_cache is not None and now - _binance_position_snapshot_cache_ms <= 1500:
        return _binance_position_snapshot_cache
    try:
        snapshot = await binance_client.position_snapshot()
        if snapshot is not None:
            _binance_position_snapshot_cache = snapshot
            _binance_position_snapshot_cache_ms = now
    except Exception as exc:  # noqa: BLE001
        logger.warning("Binance position snapshot unavailable: %s", str(exc)[:160])
    return _binance_position_snapshot_cache


def _now_ms() -> int:
    return int(asyncio.get_running_loop().time() * 1000)


@app.put("/config", response_model=RuntimeConfig)
async def update_config(payload: RuntimeConfigUpdate) -> RuntimeConfig:
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    for field, value in updates.items():
        setattr(settings, field, value)
    update_local_config_file(updates)
    return _runtime_config()


@app.post("/mt4/tick")
async def mt4_tick(payload: Mt4Tick, x_mt4_token: str | None = Header(default=None)) -> dict:
    if not mt4_bridge.token_ok(x_mt4_token or payload.token):
        raise HTTPException(status_code=403, detail="invalid MT4 token")
    quote = mt4_bridge.update_tick(payload)
    return {"status": "ok", "symbol": quote.symbol, "timestamp_ms": quote.timestamp_ms}


@app.post("/mt4/history")
async def mt4_history(payload: Mt4HistoryPayload, x_mt4_token: str | None = Header(default=None)) -> dict:
    if not mt4_bridge.token_ok(x_mt4_token or payload.token):
        raise HTTPException(status_code=403, detail="invalid MT4 token")
    if payload.symbol != settings.mt4_symbol:
        raise HTTPException(status_code=400, detail="MT4 品种不匹配")
    saved = storage.upsert_bars("mt4", payload.symbol, payload.interval, payload.bars)
    return {"status": "ok", "saved": saved}


@app.post("/mt4/order-history")
async def mt4_order_history(payload: Mt4OrderHistoryPayload, x_mt4_token: str | None = Header(default=None)) -> dict:
    if not mt4_bridge.token_ok(x_mt4_token or payload.token):
        raise HTTPException(status_code=403, detail="invalid MT4 token")
    if payload.symbol != settings.mt4_symbol:
        raise HTTPException(status_code=400, detail="MT4 品种不匹配")
    saved = storage.upsert_mt4_closed_orders(payload.orders)
    return {"status": "ok", "saved": saved}


@app.get("/mt4/command")
async def mt4_command(token: str | None = Query(default=None), x_mt4_token: str | None = Header(default=None)) -> dict:
    if not mt4_bridge.token_ok(x_mt4_token or token):
        raise HTTPException(status_code=403, detail="invalid MT4 token")
    return mt4_bridge.next_command()


@app.post("/mt4/report")
async def mt4_report(payload: Mt4Report, x_mt4_token: str | None = Header(default=None)) -> dict:
    if not mt4_bridge.token_ok(x_mt4_token or payload.token):
        raise HTTPException(status_code=403, detail="invalid MT4 token")
    mt4_bridge.submit_report(payload)
    return {"status": "ok"}


@app.post("/paper/binance/book")
async def paper_binance_book(bid: Decimal, ask: Decimal) -> dict:
    if not isinstance(binance_client, PaperBinanceClient):
        raise HTTPException(status_code=400, detail="only available in paper mode")
    binance_client.set_quote(bid, ask)
    return {"status": "ok", "bid": str(bid), "ask": str(ask)}


@app.post("/paper/binance/fill/{order_id}")
async def paper_binance_fill(order_id: str, quantity: Decimal, price: Decimal | None = None) -> dict:
    if not isinstance(binance_client, PaperBinanceClient):
        raise HTTPException(status_code=400, detail="only available in paper mode")
    order = await binance_client.simulate_fill(order_id, quantity, price)
    return order.model_dump(mode="json")


@app.post("/control/resume")
async def resume() -> dict:
    if not settings.is_dry_run:
        await _assert_resume_safe()
    strategy.resume()
    _persist_runtime_state()
    return {"status": "ok", "state": strategy.state}


@app.post("/control/paper/clear")
async def clear_paper_state() -> dict:
    if not settings.is_dry_run:
        raise HTTPException(status_code=400, detail="实盘模式不允许清理运行持仓状态")
    if isinstance(binance_client, PaperBinanceClient):
        binance_client.clear_orders()
    strategy.clear_runtime_state()
    _persist_runtime_state()
    return {"status": "ok", "state": strategy.state}


@app.post("/control/live/start")
async def start_live_mode() -> dict:
    _assert_live_preflight()
    if isinstance(binance_client, PaperBinanceClient):
        binance_client.clear_orders()
    strategy.clear_runtime_state()
    _persist_runtime_state()
    update_mode_file(live_trading=True, paper_mode=False)
    asyncio.create_task(_restart_after_response())
    return {"status": "restarting", "mode": "live", "message": "实盘模式已写入，服务正在重启"}


@app.post("/control/live/stop")
async def stop_live_mode() -> dict:
    if not settings.is_dry_run:
        await _prepare_live_stop()
    update_mode_file(live_trading=False, paper_mode=True)
    asyncio.create_task(_restart_after_response())
    return {"status": "restarting", "mode": "paper", "message": "已切回模拟模式，服务正在重启"}


@app.get("/analysis/spread", response_model=SpreadAnalysis)
async def spread_analysis(
    days: int = Query(default=7, ge=1, le=30),
    interval: str = Query(default="1m", pattern="^(1m|5m|15m|1h)$"),
    threshold: Decimal = Query(default=Decimal("0.50"), gt=0),
) -> SpreadAnalysis:
    return await build_spread_analysis(settings, storage, days, interval, threshold)


@app.get("/history/trades", response_model=TradeHistoryResponse)
async def trade_history(days: int = Query(default=7, ge=1, le=30)) -> TradeHistoryResponse:
    end_ms = utc_now_ms()
    start_ms = end_ms - days * 86_400_000
    mt4_orders = storage.get_mt4_closed_orders(settings.mt4_symbol, start_ms, end_ms, limit=100)
    try:
        binance_trades = await binance_client.user_trades(start_ms, end_ms, limit=1000)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Binance trade history unavailable: %s", str(exc)[:160])
        binance_trades = []
    try:
        funding_rows = await binance_client.funding_income(start_ms, end_ms, limit=1000)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Binance funding history unavailable: %s", str(exc)[:160])
        funding_rows = []
    return TradeHistoryResponse(
        source="币安真实成交/资金费 + MT4 EA 上传的账户历史",
        items=_build_trade_history(mt4_orders, binance_trades, funding_rows),
    )


def _build_trade_history(mt4_orders: list[Mt4ClosedOrder], binance_trades: list[dict], funding_rows: list[dict] | None = None) -> list[TradeHistoryItem]:
    items: list[TradeHistoryItem] = []
    exit_allocations = _allocate_exit_trades(mt4_orders, binance_trades)
    for mt4_order in mt4_orders:
        quantity_oz = mt4_order.lots * settings.mt4_lot_size_oz
        entry_side = Side.SELL if mt4_order.side == Side.BUY else Side.BUY
        exit_side = Side.BUY if entry_side == Side.SELL else Side.SELL
        entry_trade = _match_binance_trade(binance_trades, entry_side, quantity_oz, mt4_order.open_time_ms)
        exit_trade = exit_allocations.get(mt4_order.ticket) or _match_binance_trade(binance_trades, exit_side, quantity_oz, mt4_order.close_time_ms)
        binance_realized = _decimal_field(exit_trade, "realizedPnl") if exit_trade else None
        entry_commission = (_decimal_field(entry_trade, "commission") if entry_trade else None) or Decimal("0")
        exit_commission = (_decimal_field(exit_trade, "commission") if exit_trade else None) or Decimal("0")
        binance_commission = entry_commission + exit_commission
        mt4_total = mt4_order.profit + mt4_order.swap + mt4_order.commission
        net = None
        if binance_realized is not None:
            net = binance_realized - binance_commission + mt4_total
        status = "完整真实数据" if entry_trade and exit_trade else "缺少币安成交匹配"
        items.append(
            TradeHistoryItem(
                open_time_ms=mt4_order.open_time_ms,
                close_time_ms=mt4_order.close_time_ms,
                quantity_oz=quantity_oz,
                binance_entry_order_id=str(entry_trade.get("orderId")) if entry_trade else None,
                binance_entry_side=entry_side if entry_trade else None,
                binance_entry_price=_decimal_field(entry_trade, "price") if entry_trade else None,
                binance_exit_order_id=str(exit_trade.get("orderId")) if exit_trade else None,
                binance_exit_side=exit_side if exit_trade else None,
                binance_exit_price=_decimal_field(exit_trade, "price") if exit_trade else None,
                binance_realized_pnl=binance_realized,
                binance_commission=binance_commission if entry_trade or exit_trade else None,
                mt4_ticket=mt4_order.ticket,
                mt4_tickets=[mt4_order.ticket],
                mt4_side=mt4_order.side,
                mt4_lots=mt4_order.lots,
                mt4_open_price=mt4_order.open_price,
                mt4_close_price=mt4_order.close_price,
                mt4_profit=mt4_order.profit,
                mt4_swap=mt4_order.swap,
                mt4_commission=mt4_order.commission,
                net_pnl=net,
                status=status,
            )
        )
    return _apply_funding_income(_group_trade_history_items(items), funding_rows or [])


def _apply_funding_income(items: list[TradeHistoryItem], funding_rows: list[dict]) -> list[TradeHistoryItem]:
    if not funding_rows:
        return items
    updates: dict[int, Decimal] = {}
    for row in funding_rows:
        income = _decimal_field(row, "income")
        income_time = _int_field(row, "time")
        if income is None or income_time is None:
            continue
        for index, item in enumerate(items):
            if item.open_time_ms is None or item.close_time_ms is None:
                continue
            if item.open_time_ms - 60_000 <= income_time <= item.close_time_ms + 60_000:
                updates[index] = updates.get(index, Decimal("0")) + income
                break
    if not updates:
        return items
    result = list(items)
    for index, funding in updates.items():
        item = result[index]
        net = item.net_pnl + funding if item.net_pnl is not None else None
        result[index] = item.model_copy(update={"binance_funding_income": funding, "net_pnl": net})
    return result


def _group_trade_history_items(items: list[TradeHistoryItem]) -> list[TradeHistoryItem]:
    grouped: dict[tuple, list[TradeHistoryItem]] = {}
    for item in items:
        if item.binance_exit_order_id:
            key = ("binance_exit", item.binance_exit_order_id)
        else:
            close_bucket = (item.close_time_ms or 0) // 120_000
            key = ("mt4_batch", item.mt4_side.value if item.mt4_side else None, close_bucket)
        grouped.setdefault(key, []).append(item)
    rows = [_merge_trade_group(group) for group in grouped.values()]
    rows.sort(key=lambda item: item.close_time_ms or 0, reverse=True)
    return rows


def _merge_trade_group(group: list[TradeHistoryItem]) -> TradeHistoryItem:
    if len(group) == 1:
        return group[0]
    quantity = _sum_optional_decimal([item.quantity_oz for item in group]) or Decimal("0")
    mt4_lots = _sum_optional_decimal([item.mt4_lots for item in group])
    binance_commission = _sum_optional_decimal([item.binance_commission for item in group])
    mt4_profit = _sum_optional_decimal([item.mt4_profit for item in group])
    mt4_swap = _sum_optional_decimal([item.mt4_swap for item in group])
    mt4_commission = _sum_optional_decimal([item.mt4_commission for item in group])
    binance_realized = _sum_optional_decimal([item.binance_realized_pnl for item in group], require_all=True)
    net_pnl = _sum_optional_decimal([item.net_pnl for item in group], require_all=True)
    tickets = [ticket for item in group for ticket in (item.mt4_tickets or ([] if item.mt4_ticket is None else [item.mt4_ticket]))]
    complete = all(item.status == "完整真实数据" for item in group)
    return TradeHistoryItem(
        open_time_ms=min((item.open_time_ms for item in group if item.open_time_ms is not None), default=None),
        close_time_ms=max((item.close_time_ms for item in group if item.close_time_ms is not None), default=None),
        quantity_oz=quantity,
        binance_entry_order_id=_join_unique([item.binance_entry_order_id for item in group]),
        binance_entry_side=_same_value([item.binance_entry_side for item in group]),
        binance_entry_price=_weighted_price([(item.binance_entry_price, item.quantity_oz) for item in group]),
        binance_exit_order_id=_join_unique([item.binance_exit_order_id for item in group]),
        binance_exit_side=_same_value([item.binance_exit_side for item in group]),
        binance_exit_price=_weighted_price([(item.binance_exit_price, item.quantity_oz) for item in group]),
        binance_realized_pnl=binance_realized,
        binance_commission=binance_commission,
        mt4_ticket=None,
        mt4_tickets=tickets,
        mt4_side=_same_value([item.mt4_side for item in group]),
        mt4_lots=mt4_lots,
        mt4_open_price=_weighted_price([(item.mt4_open_price, item.mt4_lots) for item in group]),
        mt4_close_price=_weighted_price([(item.mt4_close_price, item.mt4_lots) for item in group]),
        mt4_profit=mt4_profit,
        mt4_swap=mt4_swap,
        mt4_commission=mt4_commission,
        net_pnl=net_pnl,
        status=f"{'完整真实数据' if complete else '部分缺少币安成交匹配'}（{len(group)}张合并）",
    )


def _sum_optional_decimal(values: list[Decimal | None], require_all: bool = False) -> Decimal | None:
    if require_all and any(value is None for value in values):
        return None
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present, Decimal("0"))


def _weighted_price(values: list[tuple[Decimal | None, Decimal | None]]) -> Decimal | None:
    total_qty = sum((qty for price, qty in values if price is not None and qty is not None), Decimal("0"))
    if total_qty <= 0:
        return None
    return sum(((price or Decimal("0")) * (qty or Decimal("0")) for price, qty in values if price is not None and qty is not None), Decimal("0")) / total_qty


def _same_value(values: list):
    present = [value for value in values if value is not None]
    if not present:
        return None
    first = present[0]
    return first if all(value == first for value in present) else None


def _join_unique(values: list[str | None]) -> str | None:
    seen = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return " / ".join(seen) if seen else None


def _allocate_exit_trades(mt4_orders: list[Mt4ClosedOrder], trades: list[dict]) -> dict[int, dict]:
    allocations: dict[int, dict] = {}
    for trade in sorted(trades, key=lambda item: _int_field(item, "time") or 0):
        side_value = str(trade.get("side") or "")
        if side_value not in {Side.BUY.value, Side.SELL.value}:
            continue
        trade_qty = _decimal_field(trade, "qty")
        trade_time = _int_field(trade, "time")
        if trade_qty is None or trade_qty <= 0 or trade_time is None:
            continue
        candidates = []
        for order in mt4_orders:
            if order.ticket in allocations:
                continue
            quantity_oz = order.lots * settings.mt4_lot_size_oz
            entry_side = Side.SELL if order.side == Side.BUY else Side.BUY
            exit_side = Side.BUY if entry_side == Side.SELL else Side.SELL
            if side_value != exit_side.value:
                continue
            distance = abs(trade_time - order.close_time_ms)
            if distance <= 600_000:
                candidates.append((distance, order, quantity_oz))
        if not candidates:
            continue
        candidates.sort(key=lambda item: item[0])
        used_qty = Decimal("0")
        selected = []
        for _, order, quantity_oz in candidates:
            if used_qty + quantity_oz > trade_qty + Decimal("0.000001"):
                continue
            selected.append((order, quantity_oz))
            used_qty += quantity_oz
            if abs(used_qty - trade_qty) <= Decimal("0.000001"):
                break
        if not selected:
            continue
        realized = _decimal_field(trade, "realizedPnl") or Decimal("0")
        commission = _decimal_field(trade, "commission") or Decimal("0")
        for order, quantity_oz in selected:
            ratio = quantity_oz / trade_qty
            allocated = dict(trade)
            allocated["qty"] = str(quantity_oz)
            allocated["realizedPnl"] = str(realized * ratio)
            allocated["commission"] = str(commission * ratio)
            allocations[order.ticket] = allocated
    return allocations


def _match_binance_trade(trades: list[dict], side: Side, quantity: Decimal, target_time_ms: int) -> dict | None:
    candidates = []
    grouped: dict[str, list[dict]] = {}
    for trade in trades:
        if str(trade.get("side")) != side.value:
            continue
        qty = _decimal_field(trade, "qty")
        if qty is None:
            continue
        trade_time = _int_field(trade, "time")
        if trade_time is None:
            continue
        distance = abs(trade_time - target_time_ms)
        if distance > 600_000:
            continue
        order_id = str(trade.get("orderId") or trade.get("order_id") or f"trade-{trade_time}")
        grouped.setdefault(order_id, []).append(trade)
        if abs(qty - quantity) <= Decimal("0.000001"):
            candidates.append((distance, trade))
    if not candidates:
        grouped_candidates = []
        for parts in grouped.values():
            total_qty = sum((_decimal_field(part, "qty") or Decimal("0") for part in parts), Decimal("0"))
            if abs(total_qty - quantity) > Decimal("0.000001"):
                continue
            closest = min(abs((_int_field(part, "time") or target_time_ms) - target_time_ms) for part in parts)
            grouped_candidates.append((closest, _combine_trade_parts(parts)))
        if not grouped_candidates:
            return None
        grouped_candidates.sort(key=lambda item: item[0])
        return grouped_candidates[0][1]
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _combine_trade_parts(parts: list[dict]) -> dict:
    first = dict(parts[0])
    total_qty = sum((_decimal_field(part, "qty") or Decimal("0") for part in parts), Decimal("0"))
    if total_qty <= 0:
        return first
    notional = sum(((_decimal_field(part, "price") or Decimal("0")) * (_decimal_field(part, "qty") or Decimal("0")) for part in parts), Decimal("0"))
    realized = sum((_decimal_field(part, "realizedPnl") or Decimal("0") for part in parts), Decimal("0"))
    commission = sum((_decimal_field(part, "commission") or Decimal("0") for part in parts), Decimal("0"))
    first["qty"] = str(total_qty)
    first["price"] = str(notional / total_qty)
    first["realizedPnl"] = str(realized)
    first["commission"] = str(commission)
    return first


def _decimal_field(data: dict | None, key: str) -> Decimal | None:
    if not data or data.get(key) is None:
        return None
    return Decimal(str(data[key]))


def _int_field(data: dict | None, key: str) -> int | None:
    if not data or data.get(key) is None:
        return None
    return int(data[key])


def _assert_live_preflight() -> None:
    if not settings.binance_api_key or not settings.binance_api_secret:
        raise HTTPException(status_code=400, detail="币安密钥未配置")
    if not mt4_bridge.connected():
        raise HTTPException(status_code=400, detail="MT4 未连接")
    if not binance_client.latest_quote():
        raise HTTPException(status_code=400, detail="币安报价未连接")
    if binance_client.maker_fee_rate is None:
        raise HTTPException(status_code=400, detail="币安挂单手续费率未读取")
    for quote in (binance_client.latest_quote(), mt4_bridge.latest_quote()):
        check = risk.quote_fresh(quote)
        if not check.ok:
            raise HTTPException(status_code=400, detail=f"报价异常：{check.reason}")


async def _prepare_live_stop() -> None:
    await _cancel_orphan_arb_orders("manual_stop")
    try:
        qty = await binance_client.position_quantity()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"停止实盘前检查币安持仓失败：{str(exc)[:160]}") from exc
    if qty != 0 and strategy.open_pair is None:
        raise HTTPException(status_code=400, detail=f"币安仍有 {settings.binance_symbol} 持仓 {qty}，不能直接停止；请先确认并平仓")
    if strategy.open_pair is not None:
        raise HTTPException(status_code=400, detail="当前有组合持仓，不能直接停止实盘；请先完成平仓")
    order = strategy.active_order
    if order is None:
        return
    remote_order = await _fetch_stop_order(order.order_id)
    checked_order = remote_order or order
    if checked_order.executed_qty > 0 or checked_order.status in {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED}:
        raise HTTPException(status_code=400, detail="币安挂单已有成交数量，不能直接停止实盘；需要先完成 MT4 对冲或紧急平仓")
    if remote_order is not None and remote_order.status not in {OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED}:
        await _cancel_stop_order(remote_order.order_id)
    strategy.clear_runtime_state()
    _persist_runtime_state()


async def _assert_resume_safe() -> None:
    await _cancel_orphan_arb_orders("resume")
    try:
        qty = await binance_client.position_quantity()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"恢复前检查币安持仓失败：{str(exc)[:160]}") from exc
    if strategy.open_pair is None and qty != 0:
        raise HTTPException(status_code=400, detail=f"币安仍有 {settings.binance_symbol} 持仓 {qty}，不能恢复自动挂单")


async def _fetch_stop_order(order_id: str):
    try:
        return await binance_client.get_order(order_id)
    except BinanceError as exc:
        if _binance_order_missing(exc):
            return None
        raise HTTPException(status_code=400, detail=f"停止实盘前查询币安挂单失败：{str(exc)[:160]}") from exc


async def _cancel_stop_order(order_id: str) -> None:
    try:
        await binance_client.cancel_order(order_id)
    except BinanceError as exc:
        if _binance_order_missing(exc):
            return
        raise HTTPException(status_code=400, detail=f"停止实盘前撤销币安挂单失败：{str(exc)[:160]}") from exc


def _binance_order_missing(exc: BinanceError) -> bool:
    text = str(exc)
    return "-2013" in text or "-2011" in text or "Order does not exist" in text or "Unknown order" in text


async def _restart_after_response() -> None:
    await asyncio.sleep(0.4)
    os._exit(0)


async def _strategy_loop() -> None:
    while True:
        try:
            if not await _reconcile_open_pair_live_state():
                await strategy.step()
        except Exception as exc:  # noqa: BLE001
            strategy.last_error = str(exc)[:240]
            logger.exception("strategy loop error")
        _persist_runtime_state()
        await asyncio.sleep(settings.loop_interval_ms / 1000)


async def _reconcile_open_pair_live_state() -> bool:
    global _live_pair_reconcile_ms
    if settings.is_dry_run or strategy.open_pair is None:
        return False
    if strategy.active_order is not None or strategy.state not in {StrategyState.PAIR_OPEN, StrategyState.PAUSED}:
        return False
    if not mt4_bridge.connected():
        return False
    now = _now_ms()
    if now - _live_pair_reconcile_ms < 2000:
        return False
    _live_pair_reconcile_ms = now
    try:
        binance_qty = await binance_client.position_quantity()
    except Exception as exc:  # noqa: BLE001
        strategy.state = StrategyState.PAUSED
        strategy.last_error = f"组合实盘对账失败，已暂停：{str(exc)[:120]}"
        storage.record_event("open_pair_live_reconcile_failed", {"error": str(exc)[:160]})
        return True
    mt4_positions = mt4_bridge.positions()
    action = open_pair_live_reconcile_action(strategy.open_pair, binance_qty, mt4_positions, settings.mt4_symbol)
    if action == "clear":
        pair = strategy.open_pair
        storage.record_event(
            "manual_flat_pair_cleared",
            {
                "pair_id": pair.pair_id,
                "binance_position_qty": str(binance_qty),
                "mt4_positions": [
                    {"ticket": position.ticket, "symbol": position.symbol, "side": position.side.value, "lots": str(position.lots)}
                    for position in mt4_positions
                ],
            },
        )
        strategy.clear_runtime_state()
        _persist_runtime_state()
        return True
    if action == "pause":
        strategy.state = StrategyState.PAUSED
        strategy.last_error = "组合持仓与实盘不一致：币安或 MT4 已有一侧为空，已暂停，请人工确认"
        storage.record_event(
            "open_pair_live_mismatch_paused",
            {
                "pair_id": strategy.open_pair.pair_id,
                "binance_position_qty": str(binance_qty),
                "mt4_positions": [
                    {"ticket": position.ticket, "symbol": position.symbol, "side": position.side.value, "lots": str(position.lots)}
                    for position in mt4_positions
                ],
            },
        )
        return True
    return False


def _load_runtime_state() -> None:
    if not RUNTIME_STATE_PATH.exists():
        return
    try:
        data = json.loads(RUNTIME_STATE_PATH.read_text(encoding="utf-8"))
        pair_data = data.get("open_pair")
        if pair_data:
            strategy.open_pair = OpenPair.model_validate(pair_data)
            state_value = data.get("state") or StrategyState.PAIR_OPEN.value
            try:
                restored_state = StrategyState(state_value)
            except ValueError:
                restored_state = StrategyState.PAUSED
            strategy.state = restored_state if restored_state in {StrategyState.PAIR_OPEN, StrategyState.PAUSED} else StrategyState.PAUSED
            strategy.last_error = data.get("last_error")
            storage.record_event("runtime_state_restored", {"pair_id": strategy.open_pair.pair_id, "state": strategy.state.value})
    except Exception as exc:  # noqa: BLE001
        strategy.state = StrategyState.PAUSED
        strategy.last_error = "读取运行状态失败，已暂停自动挂单"
        logger.warning("failed to load runtime state: %s", str(exc)[:160])


def _persist_runtime_state() -> None:
    global _runtime_state_cache
    if strategy.open_pair is None:
        if _runtime_state_cache is not None or RUNTIME_STATE_PATH.exists():
            RUNTIME_STATE_PATH.unlink(missing_ok=True)
        _runtime_state_cache = None
        return
    payload = {
        "state": strategy.state.value,
        "last_error": strategy.last_error,
        "open_pair": strategy.open_pair.model_dump(mode="json"),
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if text == _runtime_state_cache:
        return
    RUNTIME_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_STATE_PATH.write_text(text + "\n", encoding="utf-8")
    _runtime_state_cache = text


def _runtime_config() -> RuntimeConfig:
    return RuntimeConfig(
        binance_api_configured=bool(settings.binance_api_key and settings.binance_api_secret),
        config_files=[str(path) for path in existing_env_paths()],
        mt4_script_path=str((MT4_DIR / "ArbBridgeEA.mq4").resolve()),
        binance_leverage=settings.binance_leverage,
        binance_entry_offset_usd=settings.binance_entry_offset_usd,
        open_min_edge=settings.open_min_edge,
        cancel_min_edge=settings.cancel_min_edge,
        close_max_spread=settings.close_max_spread,
        close_profit_usd_per_oz=settings.close_profit_usd_per_oz,
        max_pair_age_minutes=settings.max_pair_age_minutes,
        aged_close_profit_usd_per_oz=settings.aged_close_profit_usd_per_oz,
        min_locked_edge=settings.min_locked_edge,
        entry_confirm_ms=settings.entry_confirm_ms,
        min_order_live_ms=settings.min_order_live_ms,
        requote_cooldown_ms=settings.requote_cooldown_ms,
        max_order_age_ms=settings.max_order_age_ms,
        max_quote_age_ms=settings.max_quote_age_ms,
        max_hedge_delay_ms=settings.max_hedge_delay_ms,
        max_unhedged_loss_usd_per_oz=settings.max_unhedged_loss_usd_per_oz,
        daily_loss_limit_usdt=settings.daily_loss_limit_usdt,
        add_edge_growth_usd=settings.add_edge_growth_usd,
        max_add_count=settings.max_add_count,
        negative_swap_close_before_minutes=settings.negative_swap_close_before_minutes,
        target_oz=settings.target_oz,
        mt4_lot_size_oz=settings.mt4_lot_size_oz,
        mt4_slippage_points=settings.mt4_slippage_points,
        loop_interval_ms=settings.loop_interval_ms,
        paper_auto_fill=settings.paper_auto_fill,
        paper_fill_delay_ms=settings.paper_fill_delay_ms,
    )


async def _position_metrics() -> PositionMetrics:
    funding = binance_client.latest_funding()
    pair = strategy.open_pair
    binance_quote = binance_client.latest_quote()
    mt4_quote = mt4_bridge.latest_quote()
    swap_info = mt4_bridge.latest_swap_info()
    metrics = PositionMetrics(
        binance_funding_rate=funding.funding_rate if funding else None,
        binance_next_funding_time_ms=funding.next_funding_time_ms if funding else None,
        mt4_next_rollover_time_ms=swap_info.next_rollover_time_ms,
        mt4_swap_long_per_lot=swap_info.swap_long_per_lot,
        mt4_swap_short_per_lot=swap_info.swap_short_per_lot,
        mt4_swap_type=swap_info.swap_type,
    )
    if not pair:
        return metrics

    qty = pair.quantity_oz
    binance_snapshot = await _binance_position_snapshot()
    binance_entry_price = _actual_binance_entry_price(pair, binance_snapshot)
    mt4_entry_price, mt4_lots = _mt4_average_entry_price(pair)
    funding_estimate = _estimate_binance_funding(pair, qty, funding, binance_quote)
    mt4_swap_estimate = _estimate_mt4_swap(pair, qty, swap_info)
    accrued_funding = await _binance_accrued_funding(pair)
    accrued_swap = _mt4_accrued_swap(pair)
    gross = _estimate_close_gross(pair, binance_quote, mt4_quote, binance_entry_price, mt4_entry_price)
    fees = _estimate_binance_fees(pair, binance_quote, binance_entry_price)
    actual_entry_spread = _actual_entry_spread(pair, binance_entry_price, mt4_entry_price)
    current_exit_spread = _current_exit_spread(pair, binance_quote, mt4_quote)
    profitable_spread_threshold = _profitable_spread_threshold(pair, actual_entry_spread, accrued_funding, accrued_swap, fees)
    exit_follow_buffer = _exit_follow_buffer_usd_per_oz(swap_info)
    close_profit = _effective_close_profit_usd_per_oz(pair)
    dynamic_close_spread = _dynamic_close_spread(profitable_spread_threshold, exit_follow_buffer, close_profit)
    net = None
    if gross is not None and fees is not None:
        net = gross - fees
        if accrued_funding is not None:
            net += accrued_funding
        if funding_estimate is not None:
            net += funding_estimate
        if mt4_swap_estimate is not None:
            net += mt4_swap_estimate
        if accrued_swap is not None:
            net += accrued_swap
    return metrics.model_copy(
        update={
            "binance_position_entry_price": binance_snapshot.entry_price if binance_snapshot else None,
            "binance_position_break_even_price": binance_snapshot.break_even_price if binance_snapshot else None,
            "binance_position_mark_price": binance_snapshot.mark_price if binance_snapshot else None,
            "binance_unrealized_pnl": binance_snapshot.unrealized_pnl if binance_snapshot else None,
            "mt4_position_entry_price": mt4_entry_price,
            "mt4_position_lots": mt4_lots,
            "actual_entry_spread": actual_entry_spread,
            "current_exit_spread": current_exit_spread,
            "profitable_spread_threshold": profitable_spread_threshold,
            "dynamic_close_spread": dynamic_close_spread,
            "close_profit_usd_per_oz": close_profit,
            "exit_follow_buffer_usd_per_oz": exit_follow_buffer,
            "binance_accrued_funding": accrued_funding,
            "binance_funding_estimate": funding_estimate,
            "mt4_swap_estimate": mt4_swap_estimate,
            "mt4_accrued_swap": accrued_swap,
            "estimated_close_gross": gross,
            "estimated_fees": fees,
            "estimated_close_net": net,
        }
    )


async def _binance_accrued_funding(pair) -> Decimal | None:
    global _binance_accrued_funding_cache_pair_id
    global _binance_accrued_funding_cache_value
    global _binance_accrued_funding_cache_ms
    if settings.is_dry_run:
        return None
    now = _now_ms()
    if (
        _binance_accrued_funding_cache_pair_id == pair.pair_id
        and _binance_accrued_funding_cache_value is not None
        and now - _binance_accrued_funding_cache_ms <= 10_000
    ):
        return _binance_accrued_funding_cache_value
    try:
        rows = await binance_client.funding_income(pair.opened_ms - 60_000, utc_now_ms(), limit=1000)
        total = sum((_decimal_field(row, "income") or Decimal("0") for row in rows), Decimal("0"))
        _binance_accrued_funding_cache_pair_id = pair.pair_id
        _binance_accrued_funding_cache_value = total
        _binance_accrued_funding_cache_ms = now
        return total
    except Exception as exc:  # noqa: BLE001
        logger.warning("Binance funding income unavailable: %s", str(exc)[:160])
        if _binance_accrued_funding_cache_pair_id == pair.pair_id:
            return _binance_accrued_funding_cache_value
        return None


def _actual_binance_entry_price(pair, snapshot: BinancePositionSnapshot | None) -> Decimal:
    if snapshot and snapshot.entry_price is not None and snapshot.position_amt != 0:
        return snapshot.entry_price
    return pair.binance_entry_price


def _mt4_average_entry_price(pair) -> tuple[Decimal | None, Decimal | None]:
    matched = _matched_mt4_positions(pair)
    if not matched:
        return None, None
    total_lots = sum((position.lots for position in matched), Decimal("0"))
    if total_lots <= 0:
        return None, None
    weighted = sum((position.open_price * position.lots for position in matched), Decimal("0"))
    return weighted / total_lots, total_lots


def _matched_mt4_positions(pair) -> list:
    positions = mt4_bridge.positions()
    if not positions:
        return []
    tickets = set(pair.mt4_tickets or ([] if pair.mt4_ticket is None else [pair.mt4_ticket]))
    if tickets:
        matched = [position for position in positions if position.ticket in tickets]
        if matched:
            return matched
    expected_side = Side.BUY if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG else Side.SELL
    return [position for position in positions if position.symbol == settings.mt4_symbol and position.side == expected_side]


def _actual_entry_spread(pair, binance_entry_price: Decimal, mt4_entry_price: Decimal | None) -> Decimal | None:
    if mt4_entry_price is None:
        return None
    if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
        return binance_entry_price - mt4_entry_price
    return mt4_entry_price - binance_entry_price


def _current_exit_spread(pair, binance_quote: MarketQuote | None, mt4_quote: MarketQuote | None) -> Decimal | None:
    if not binance_quote or not mt4_quote:
        return None
    if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
        return round_down(binance_quote.bid, binance_client.filters.tick_size) - mt4_quote.bid
    return mt4_quote.ask - round_up(binance_quote.ask, binance_client.filters.tick_size)


def _profitable_spread_threshold(
    pair,
    actual_entry_spread: Decimal | None,
    accrued_funding: Decimal | None,
    accrued_swap: Decimal | None,
    fees: Decimal | None,
) -> Decimal | None:
    if actual_entry_spread is None or fees is None or pair.quantity_oz <= 0:
        return None
    adjustment = (accrued_funding or Decimal("0")) + (accrued_swap or Decimal("0")) - fees
    return actual_entry_spread + (adjustment / pair.quantity_oz)


def _dynamic_close_spread(
    profitable_spread_threshold: Decimal | None,
    exit_follow_buffer: Decimal | None = None,
    close_profit: Decimal | None = None,
) -> Decimal | None:
    if profitable_spread_threshold is None:
        return None
    return profitable_spread_threshold - (close_profit or settings.close_profit_usd_per_oz) - (exit_follow_buffer or Decimal("0"))


def _effective_close_profit_usd_per_oz(pair) -> Decimal:
    if settings.max_pair_age_minutes <= 0:
        return settings.close_profit_usd_per_oz
    age_ms = utc_now_ms() - int(pair.opened_ms)
    if age_ms >= settings.max_pair_age_minutes * 60_000:
        return min(settings.close_profit_usd_per_oz, settings.aged_close_profit_usd_per_oz)
    return settings.close_profit_usd_per_oz


def _exit_follow_buffer_usd_per_oz(swap_info) -> Decimal:
    point = swap_info.point or Decimal("0.01")
    return Decimal(settings.mt4_slippage_points) * point


def _estimate_binance_funding(pair, qty: Decimal, funding, quote: MarketQuote | None) -> Decimal | None:
    if not funding:
        return None
    price = funding.mark_price or (quote.mid if quote else None)
    if price is None:
        return None
    amount = price * qty * funding.funding_rate
    if pair.direction == PairDirection.BINANCE_LONG_MT4_SHORT:
        amount = -amount
    return amount


def _estimate_mt4_swap(pair, qty: Decimal, swap_info) -> Decimal | None:
    lots = qty / settings.mt4_lot_size_oz
    raw = swap_info.swap_long_per_lot if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG else swap_info.swap_short_per_lot
    if raw is None:
        return None
    if swap_info.swap_type == 0:
        if not swap_info.tick_value or not swap_info.tick_size or not swap_info.point:
            return raw * lots
        return raw * (swap_info.point / swap_info.tick_size) * swap_info.tick_value * lots
    return raw * lots


def _mt4_accrued_swap(pair) -> Decimal | None:
    matched = _matched_mt4_positions(pair)
    if not matched:
        return None
    return sum((position.swap for position in matched), Decimal("0"))


def _estimate_close_gross(
    pair,
    binance_quote: MarketQuote | None,
    mt4_quote: MarketQuote | None,
    binance_entry_price: Decimal | None = None,
    mt4_entry_price: Decimal | None = None,
) -> Decimal | None:
    if not binance_quote or not mt4_quote:
        return None
    qty = pair.quantity_oz
    binance_entry = binance_entry_price or pair.binance_entry_price
    mt4_entry = mt4_entry_price or pair.mt4_entry_price
    if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
        binance_exit = round_down(binance_quote.bid, binance_client.filters.tick_size)
        mt4_exit = mt4_quote.bid
        return (binance_entry - binance_exit) * qty + (mt4_exit - mt4_entry) * qty
    binance_exit = round_up(binance_quote.ask, binance_client.filters.tick_size)
    mt4_exit = mt4_quote.ask
    return (binance_exit - binance_entry) * qty + (mt4_entry - mt4_exit) * qty


def _estimate_binance_fees(pair, binance_quote: MarketQuote | None, binance_entry_price: Decimal | None = None) -> Decimal | None:
    fee_rate = binance_client.maker_fee_rate
    if fee_rate is None or not binance_quote:
        return None
    qty = pair.quantity_oz
    entry_price = binance_entry_price or pair.binance_entry_price
    if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
        exit_price = round_down(binance_quote.bid, binance_client.filters.tick_size)
    else:
        exit_price = round_up(binance_quote.ask, binance_client.filters.tick_size)
    return (entry_price * qty + exit_price * qty) * abs(fee_rate)


def _execution_plan(metrics: PositionMetrics | None = None) -> ExecutionPlanStatus:
    max_follow_seconds = Decimal(settings.max_hedge_delay_ms) / Decimal("1000")
    order = strategy.active_order
    plan = strategy.active_plan
    if order:
        follow_side = plan.mt4_hedge_side if plan else None
        if order.reduce_only:
            summary = f"币安当前平仓挂单：{_side_text(order.side)} {order.orig_qty} XAU，价格 {order.price}，状态 {_order_status_text(order.status.value)}；币安全部成交后 MT4 逐张市价平仓。"
            follow_side = Side.SELL if strategy.open_pair and strategy.open_pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG else Side.BUY
        else:
            summary = f"币安当前挂单：{_side_text(order.side)} {order.orig_qty} XAU，价格 {order.price}，状态 {_order_status_text(order.status.value)}；成交后 MT4 立即市价对冲，不检查价差保护价。"
        return ExecutionPlanStatus(
            summary=summary,
            active_binance_order=True,
            binance_order_status=order.status,
            binance_order_side=order.side,
            binance_order_price=order.price,
            binance_order_qty=order.orig_qty,
            binance_order_executed_qty=order.executed_qty,
            mt4_follow_side=follow_side,
            mt4_price_limit=None,
            max_follow_seconds=max_follow_seconds,
        )

    pair = strategy.open_pair
    binance_quote = binance_client.latest_quote()
    mt4_quote = mt4_bridge.latest_quote()
    quote_block_reason = _quote_plan_block_reason(binance_quote, mt4_quote)
    if strategy.state.value == "PAUSED":
        if pair and _strategy_paused_for_quote_issue():
            detail = f"（{_risk_reason_text(strategy.last_error or '')}）" if strategy.last_error else ""
            return ExecutionPlanStatus(
                summary=f"报价临时异常{detail}，暂时不挂单；报价恢复后会自动继续管理已有仓位的补仓和平仓，不会开独立新仓。",
                max_follow_seconds=max_follow_seconds,
            )
        if pair and binance_quote and mt4_quote:
            add_summary = _pair_add_summary(pair, binance_quote, mt4_quote, metrics)
            swap_summary = _negative_swap_close_summary(pair)
            return ExecutionPlanStatus(
                summary=f"系统因风控硬暂停，不会自动补仓或平仓；当前仍有组合持仓。{swap_summary or add_summary}",
                max_follow_seconds=max_follow_seconds,
            )
        return ExecutionPlanStatus(summary="系统因风控硬暂停，不会自动新挂单；恢复后才会继续按价差条件执行。", max_follow_seconds=max_follow_seconds)
    if pair and binance_quote and mt4_quote:
        if quote_block_reason:
            return ExecutionPlanStatus(summary=f"当前有组合持仓，但{quote_block_reason}，暂不挂平仓单。", max_follow_seconds=max_follow_seconds)
        swap_summary = _negative_swap_close_summary(pair)
        if swap_summary and swap_summary.startswith("隔夜费亏损风控已触发") and pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
            return ExecutionPlanStatus(
                summary=f"{swap_summary} 币安将挂 买入 限价 {round_down(binance_quote.bid, binance_client.filters.tick_size)}，全部成交后 MT4 逐张平仓。",
                binance_order_side=Side.BUY,
                binance_order_price=round_down(binance_quote.bid, binance_client.filters.tick_size),
                binance_order_qty=pair.quantity_oz,
                mt4_follow_side=Side.SELL,
                max_follow_seconds=max_follow_seconds,
            )
        if swap_summary and swap_summary.startswith("隔夜费亏损风控已触发") and pair.direction == PairDirection.BINANCE_LONG_MT4_SHORT:
            return ExecutionPlanStatus(
                summary=f"{swap_summary} 币安将挂 卖出 限价 {round_up(binance_quote.ask, binance_client.filters.tick_size)}，全部成交后 MT4 逐张平仓。",
                binance_order_side=Side.SELL,
                binance_order_price=round_up(binance_quote.ask, binance_client.filters.tick_size),
                binance_order_qty=pair.quantity_oz,
                mt4_follow_side=Side.BUY,
                max_follow_seconds=max_follow_seconds,
            )
        add_summary = _pair_add_summary(pair, binance_quote, mt4_quote, metrics)
        add_plan = _pair_add_plan(pair, binance_quote, mt4_quote, metrics)
        if add_plan:
            return ExecutionPlanStatus(
                summary=f"补仓条件已满足：{add_summary}；币安将同向挂 {_side_text(add_plan.binance_side)} 限价 {add_plan.limit_price}，数量 {add_plan.quantity_oz} XAU；成交后 MT4 同向 {_side_text(add_plan.mt4_hedge_side)} 市价跟随。",
                binance_order_side=add_plan.binance_side,
                binance_order_price=add_plan.limit_price,
                binance_order_qty=add_plan.quantity_oz,
                mt4_follow_side=add_plan.mt4_hedge_side,
                mt4_price_limit=None,
                max_follow_seconds=max_follow_seconds,
            )
        spread = _current_exit_spread(pair, binance_quote, mt4_quote)
        trigger_spread = metrics.dynamic_close_spread if metrics else None
        break_even = metrics.profitable_spread_threshold if metrics else None
        follow_buffer = metrics.exit_follow_buffer_usd_per_oz if metrics else None
        close_profit = metrics.close_profit_usd_per_oz if metrics and metrics.close_profit_usd_per_oz is not None else settings.close_profit_usd_per_oz
        close_ready = spread is not None and trigger_spread is not None and spread <= trigger_spread
        if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
            side = Side.BUY
            price = round_down(binance_quote.bid, binance_client.filters.tick_size)
        else:
            side = Side.SELL
            price = round_up(binance_quote.ask, binance_client.filters.tick_size)
        trigger_text = f"{trigger_spread:.4f} 美元" if trigger_spread is not None else "等待实盘入场价确认"
        break_even_text = f"{break_even:.4f} 美元" if break_even is not None else "等待确认"
        buffer_text = f"，再预留 MT4 跟随保护 {follow_buffer:.4f} 美元/盎司" if follow_buffer is not None and follow_buffer > 0 else ""
        return ExecutionPlanStatus(
            summary=f"平仓逻辑：按实盘进场价差计算，保本价差 {break_even_text}，先扣 {close_profit} 美元/盎司利润空间{buffer_text}，当前触发价差 {trigger_text}；币安挂 {_side_text(side)} 限价 {price}；当前平仓价差 {spread if spread is not None else '-'}，{'已满足' if close_ready else '未满足'}。{add_summary}",
            binance_order_side=side,
            binance_order_price=price,
            binance_order_qty=pair.quantity_oz,
            mt4_follow_side=Side.SELL if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG else Side.BUY,
            max_follow_seconds=max_follow_seconds,
        )

    if binance_quote and mt4_quote:
        if quote_block_reason:
            return ExecutionPlanStatus(summary=f"{quote_block_reason}，暂不挂开仓单。", max_follow_seconds=max_follow_seconds)
        entry_plan = build_entry_plan(settings, binance_client.filters, binance_quote, mt4_quote)
        if entry_plan:
            confirm_left = max(0, settings.entry_confirm_ms - strategy.entry_candidate_age_ms())
            confirm_text = "已通过稳定确认" if confirm_left == 0 else f"还需稳定 {confirm_left} 毫秒"
            return ExecutionPlanStatus(
                summary=f"开仓条件已满足：{confirm_text}；币安将挂 {_side_text(entry_plan.binance_side)} 限价 {entry_plan.limit_price}，数量 {entry_plan.quantity_oz} XAU；成交后 MT4 {_side_text(entry_plan.mt4_hedge_side)} 立即市价对冲，不检查价差保护价。",
                binance_order_side=entry_plan.binance_side,
                binance_order_price=entry_plan.limit_price,
                binance_order_qty=entry_plan.quantity_oz,
                mt4_follow_side=entry_plan.mt4_hedge_side,
                mt4_price_limit=None,
                max_follow_seconds=max_follow_seconds,
            )
        high_edge = binance_quote.ask - mt4_quote.ask
        low_edge = mt4_quote.bid - binance_quote.bid
        return ExecutionPlanStatus(
            summary=f"等待开仓：币安高价差 {high_edge:.4f} 美元，币安低价差 {low_edge:.4f} 美元；任一方向达到 {settings.open_min_edge} 美元并稳定 {settings.entry_confirm_ms} 毫秒才挂单，挂单偏移 {settings.binance_entry_offset_usd} 美元，挂单后低于 {settings.cancel_min_edge} 美元才考虑撤单。",
            max_follow_seconds=max_follow_seconds,
        )
    return ExecutionPlanStatus(summary="等待 Binance 和 MT4 报价齐全。", max_follow_seconds=max_follow_seconds)


def _pair_add_plan(pair, binance_quote: MarketQuote, mt4_quote: MarketQuote, metrics: PositionMetrics | None = None):
    if settings.max_add_count <= 0 or pair.add_count >= settings.max_add_count:
        return None
    base_edge = _pair_add_base_edge(pair, metrics)
    if base_edge is None:
        return None
    trigger_edge = base_edge + settings.add_edge_growth_usd
    return build_directional_entry_plan(settings, binance_client.filters, binance_quote, mt4_quote, pair.direction, trigger_edge)


def _pair_add_summary(pair, binance_quote: MarketQuote, mt4_quote: MarketQuote, metrics: PositionMetrics | None = None) -> str:
    if settings.max_add_count <= 0:
        return "补仓已关闭。"
    if pair.add_count >= settings.max_add_count:
        return f"补仓次数 {pair.add_count}/{settings.max_add_count}，已达上限。"
    base_edge = _pair_add_base_edge(pair, metrics)
    if base_edge is None:
        return "补仓基准价差缺失，暂不补仓。"
    trigger_edge = base_edge + settings.add_edge_growth_usd
    current_edge = binance_quote.ask - mt4_quote.ask if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG else mt4_quote.bid - binance_quote.bid
    label = "上次补仓实际价差" if pair.add_count > 0 else "首仓实际价差"
    return f"补仓观察：已补 {pair.add_count}/{settings.max_add_count} 次，{label} {base_edge:.4f} 美元，下次触发 {trigger_edge:.4f} 美元，当前同向价差 {current_edge:.4f} 美元。"


def _pair_add_base_edge(pair, metrics: PositionMetrics | None = None) -> Decimal | None:
    if pair.add_count == 0:
        if metrics and metrics.actual_entry_spread is not None:
            return metrics.actual_entry_spread
        mt4_entry, _lots = _mt4_average_entry_price(pair)
        return _actual_entry_spread(pair, pair.binance_entry_price, mt4_entry or pair.mt4_entry_price)
    return pair.last_add_edge or pair.base_edge


def _negative_swap_close_summary(pair) -> str | None:
    if settings.negative_swap_close_before_minutes <= 0:
        return None
    swap_info = mt4_bridge.latest_swap_info()
    next_rollover = swap_info.next_rollover_time_ms
    if next_rollover is None:
        return None
    estimate = _estimate_mt4_swap(pair, pair.quantity_oz, swap_info)
    if estimate is None or estimate >= 0:
        return None
    projected_net = _convergence_net_after_next_mt4_swap(pair, estimate)
    ms_left = next_rollover - utc_now_ms()
    lead_ms = settings.negative_swap_close_before_minutes * 60 * 1000
    if ms_left < 0 or ms_left > lead_ms:
        minutes_left = max(0, ms_left // 60000)
        net_text = f"，扣后回归净利预估 {projected_net}" if projected_net is not None else ""
        return f"MT4 下次隔夜费预估亏损 {estimate}{net_text}，距离结算约 {minutes_left} 分钟；低于 {settings.negative_swap_close_before_minutes} 分钟且回归净利不够才会提前平仓。"
    if projected_net is not None and projected_net > 0:
        return f"MT4 下次隔夜费预估亏损 {estimate}，但扣后回归净利预估 {projected_net}，仍有利润，不提前平仓。"
    minutes_left = max(0, ms_left // 60000)
    net_text = f"，扣后回归净利预估 {projected_net}" if projected_net is not None else ""
    return f"隔夜费亏损风控已触发：MT4 下次隔夜费预估 {estimate}{net_text}，距离结算约 {minutes_left} 分钟，提前平仓。"


def _convergence_net_after_next_mt4_swap(pair, next_swap: Decimal) -> Decimal | None:
    qty = pair.quantity_oz
    if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
        opening_edge = pair.binance_entry_price - pair.mt4_entry_price
    else:
        opening_edge = pair.mt4_entry_price - pair.binance_entry_price
    net = (opening_edge - settings.close_max_spread) * qty
    fee_rate = binance_client.maker_fee_rate or settings.binance_maker_fee_rate
    if fee_rate is not None:
        net -= pair.binance_entry_price * qty * abs(fee_rate) * Decimal("2")
    accrued_swap = _mt4_accrued_swap(pair)
    if accrued_swap is not None:
        net += accrued_swap
    return net + next_swap


def _quote_plan_block_reason(binance_quote: MarketQuote | None, mt4_quote: MarketQuote | None) -> str | None:
    checks = (("币安", binance_quote), ("MT4", mt4_quote))
    for label, quote in checks:
        check = risk.quote_fresh(quote)
        if not check.ok:
            return f"{label}报价未刷新（{_risk_reason_text(check.reason)}）"
    return None


def _strategy_paused_for_quote_issue() -> bool:
    reason = strategy.last_error or ""
    return reason == "quote missing" or reason.startswith("quote stale ")


def _risk_reason_text(reason: str) -> str:
    if reason.startswith("quote stale "):
        return "报价过期 " + reason.removeprefix("quote stale ")
    if reason == "quote missing":
        return "报价缺失"
    return reason


def _side_text(side: Side) -> str:
    return "买入" if side == Side.BUY else "卖出"


def _order_status_text(status: str) -> str:
    return {
        "NEW": "等待成交",
        "PARTIALLY_FILLED": "部分成交",
        "FILLED": "已成交",
        "CANCELED": "已撤单",
        "REJECTED": "已拒绝",
        "EXPIRED": "已过期",
    }.get(status, status)
