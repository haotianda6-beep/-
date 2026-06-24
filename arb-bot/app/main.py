from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse

from app.binance_client import BinanceBaseClient, BinanceError, BinanceFuturesClient, PaperBinanceClient
from app.config import Settings, existing_env_paths, load_settings, update_local_config_file, update_mode_file
from app.logger import setup_logging
from app.history import build_spread_analysis, fetch_binance_klines
from app.live_reconcile import is_transient_live_reconcile_error, open_pair_live_reconcile_action
from app.models import (
    BinancePositionSnapshot,
    EngineStatus,
    ExecutionPlanStatus,
    HistoryBar,
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
from app.v2_planner import build_gold_v2_status


setup_logging()
logger = logging.getLogger(__name__)
POSITION_RISK_CACHE_TTL_MS = 10_000
POSITION_RISK_FAILURE_RETRY_MS = 30_000
FUNDING_INCOME_CACHE_TTL_MS = 60_000
FUNDING_INCOME_FAILURE_RETRY_MS = 60_000
LIVE_PAIR_RECONCILE_INTERVAL_MS = 30_000
HISTORY_MT4_BATCH_WINDOW_MS = 180_000
HISTORY_BINANCE_ALIGN_WINDOW_MS = 900_000
HISTORY_EVENT_EXIT_LINK_WINDOW_MS = 600_000
QTY_EPSILON = Decimal("0.000001")

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
_binance_position_qty_failure_ms = 0
_binance_position_snapshot_cache: BinancePositionSnapshot | None = None
_binance_position_snapshot_cache_ms = 0
_binance_position_snapshot_failure_ms = 0
_binance_accrued_funding_cache_pair_id: str | None = None
_binance_accrued_funding_cache_value: Decimal | None = None
_binance_accrued_funding_cache_ms = 0
_binance_accrued_funding_failure_ms = 0
_runtime_state_cache: str | None = None
_live_pair_reconcile_ms = 0
_live_pair_reconcile_error_count = 0
_live_pair_operation_cooldown_until_ms = 0
_gold_v2_binance_bars_cache: list[HistoryBar] = []
_gold_v2_binance_bars_cache_ms = 0
_gold_v2_binance_bars_failure_ms = 0
_mt4_tick_bar_last_saved_ms = 0
GOLD_V2_BAR_CACHE_TTL_MS = 60_000
GOLD_V2_BAR_FAILURE_RETRY_MS = 30_000
WEB_DIR = Path(__file__).resolve().parents[1] / "web"
MT4_DIR = Path(__file__).resolve().parents[1] / "mt4"
RUNTIME_STATE_PATH = settings.sqlite_path.parent / "runtime_state.json"


@app.on_event("startup")
async def startup() -> None:
    global _loop_task
    await binance_client.start()
    _load_runtime_state()
    if settings.gold_v2_observation_only and settings.is_dry_run:
        if isinstance(binance_client, PaperBinanceClient):
            binance_client.clear_orders()
        strategy.clear_runtime_state()
        _persist_runtime_state()
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
    global _live_pair_operation_cooldown_until_ms
    if settings.is_dry_run:
        return
    await _cancel_orphan_arb_orders("startup")
    try:
        qty = await binance_client.position_quantity()
    except Exception as exc:  # noqa: BLE001
        error_text = str(exc)[:160]
        logger.warning("failed to inspect Binance position on startup: %s", error_text)
        if strategy.open_pair is not None and is_transient_live_reconcile_error(error_text):
            cooldown_ms = _binance_transient_cooldown_ms(error_text)
            _live_pair_operation_cooldown_until_ms = max(
                _live_pair_operation_cooldown_until_ms,
                _now_ms() + cooldown_ms,
            )
            strategy.state = StrategyState.PAIR_OPEN
            strategy.last_error = f"启动时币安接口临时限频，已等待冷却约 {cooldown_ms // 1000} 秒后再对账"
            storage.record_event(
                "startup_live_reconcile_transient_cooldown",
                {"error": error_text, "cooldown_ms": cooldown_ms},
            )
            return
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
    global _live_pair_operation_cooldown_until_ms
    if settings.is_dry_run:
        return
    try:
        orders = await binance_client.open_orders()
    except Exception as exc:  # noqa: BLE001
        error_text = str(exc)[:160]
        logger.warning("failed to inspect open Binance orders during %s: %s", reason, error_text)
        if strategy.open_pair is not None and is_transient_live_reconcile_error(error_text):
            cooldown_ms = _binance_transient_cooldown_ms(error_text)
            _live_pair_operation_cooldown_until_ms = max(
                _live_pair_operation_cooldown_until_ms,
                _now_ms() + cooldown_ms,
            )
            strategy.last_error = f"检查币安遗留挂单时接口临时限频，已等待冷却约 {cooldown_ms // 1000} 秒"
            storage.record_event(
                "orphan_order_check_transient_cooldown",
                {"reason": reason, "error": error_text, "cooldown_ms": cooldown_ms},
            )
            return
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
    binance_quote = binance_client.latest_quote()
    mt4_quote = mt4_bridge.latest_quote()
    gold_v2 = await _gold_v2_status(metrics, binance_quote, mt4_quote)
    return EngineStatus(
        state=strategy.state,
        live_trading=settings.live_trading,
        paper_mode=settings.paper_mode,
        binance_connected=binance_quote is not None,
        mt4_connected=mt4_bridge.connected(),
        binance_symbol=settings.binance_symbol,
        mt4_symbol=settings.mt4_symbol,
        maker_fee_rate=binance_client.maker_fee_rate,
        binance_funding=binance_client.latest_funding(),
        binance_account=await binance_client.account_snapshot(),
        mt4_account=mt4_bridge.account_snapshot(),
        binance_position_qty=await _binance_position_quantity(),
        mt4_positions=mt4_bridge.positions(),
        binance_quote=binance_quote,
        mt4_quote=mt4_quote,
        open_pair=strategy.open_pair,
        position_metrics=metrics,
        gold_v2=gold_v2,
        execution_plan=_execution_plan(metrics),
        last_error=strategy.last_error,
        config=_runtime_config(),
    )


async def _binance_position_quantity(force: bool = False) -> Decimal | None:
    global _binance_position_qty_cache, _binance_position_qty_cache_ms, _binance_position_qty_failure_ms
    snapshot = await _binance_position_snapshot(force=force)
    if snapshot is not None:
        return snapshot.position_amt
    now = _now_ms()
    if not force and _binance_position_qty_cache is not None and now - _binance_position_qty_cache_ms <= POSITION_RISK_CACHE_TTL_MS:
        return _binance_position_qty_cache
    if not force and now - _binance_position_qty_failure_ms <= POSITION_RISK_FAILURE_RETRY_MS:
        return _binance_position_qty_cache
    try:
        _binance_position_qty_cache = await binance_client.position_quantity()
        _binance_position_qty_cache_ms = now
        _binance_position_qty_failure_ms = 0
    except Exception as exc:  # noqa: BLE001
        _binance_position_qty_failure_ms = now
        logger.warning("Binance position quantity unavailable: %s", str(exc)[:160])
        if force:
            raise
    return _binance_position_qty_cache


async def _binance_position_snapshot(force: bool = False) -> BinancePositionSnapshot | None:
    global _binance_position_snapshot_cache, _binance_position_snapshot_cache_ms, _binance_position_snapshot_failure_ms
    now = _now_ms()
    if not force and _binance_position_snapshot_cache is not None and now - _binance_position_snapshot_cache_ms <= POSITION_RISK_CACHE_TTL_MS:
        return _binance_position_snapshot_cache
    if not force and now - _binance_position_snapshot_failure_ms <= POSITION_RISK_FAILURE_RETRY_MS:
        return _binance_position_snapshot_cache
    try:
        snapshot = await binance_client.position_snapshot()
        if snapshot is not None:
            _binance_position_snapshot_cache = snapshot
            _binance_position_snapshot_cache_ms = now
            _binance_position_snapshot_failure_ms = 0
    except Exception as exc:  # noqa: BLE001
        _binance_position_snapshot_failure_ms = now
        logger.warning("Binance position snapshot unavailable: %s", str(exc)[:160])
        if force:
            raise
    return _binance_position_snapshot_cache


def _now_ms() -> int:
    return int(asyncio.get_running_loop().time() * 1000)


async def _gold_v2_status(
    metrics: PositionMetrics,
    binance_quote: MarketQuote | None,
    mt4_quote: MarketQuote | None,
) -> dict:
    try:
        binance_bars = await _gold_v2_recent_binance_bars()
        return build_gold_v2_status(
            settings=settings,
            storage=storage,
            filters=binance_client.filters,
            binance_quote=binance_quote,
            mt4_quote=mt4_quote,
            binance_bars=binance_bars,
            open_pair=strategy.open_pair,
            metrics=metrics,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("gold v2 status unavailable: %s", str(exc)[:160])
        return {
            "mode": "只读观察",
            "auto_trade_enabled": False,
            "execution_enabled": False,
            "add_enabled": False,
            "reason": f"新版观察计划暂时不可用：{str(exc)[:120]}",
        }


async def _gold_v2_recent_binance_bars() -> list[HistoryBar]:
    global _gold_v2_binance_bars_cache, _gold_v2_binance_bars_cache_ms, _gold_v2_binance_bars_failure_ms
    now = _now_ms()
    if now - _gold_v2_binance_bars_cache_ms <= GOLD_V2_BAR_CACHE_TTL_MS:
        return _gold_v2_binance_bars_cache
    if now - _gold_v2_binance_bars_failure_ms <= GOLD_V2_BAR_FAILURE_RETRY_MS:
        return _gold_v2_binance_bars_cache
    end_ms = utc_now_ms()
    start_ms = end_ms - 30 * 60 * 1000
    try:
        bars = await asyncio.wait_for(fetch_binance_klines(settings, "1m", start_ms, end_ms), timeout=5)
    except Exception as exc:  # noqa: BLE001
        _gold_v2_binance_bars_failure_ms = now
        logger.warning("gold v2 Binance bars unavailable: %s", str(exc)[:160])
        return _gold_v2_binance_bars_cache
    _gold_v2_binance_bars_cache = bars
    _gold_v2_binance_bars_cache_ms = now
    _gold_v2_binance_bars_failure_ms = 0
    return _gold_v2_binance_bars_cache


def _record_mt4_tick_bar(quote: MarketQuote) -> None:
    global _mt4_tick_bar_last_saved_ms
    now = _now_ms()
    if now - _mt4_tick_bar_last_saved_ms < 1000:
        return
    _mt4_tick_bar_last_saved_ms = now
    open_time_ms = quote.timestamp_ms - (quote.timestamp_ms % 60_000)
    price = quote.bid
    storage.upsert_bars(
        "mt4",
        quote.symbol,
        "1m",
        [
            HistoryBar(
                open_time_ms=open_time_ms,
                open=price,
                high=price,
                low=price,
                close=price,
            )
        ],
    )


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
    _record_mt4_tick_bar(quote)
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
    history_client: BinanceBaseClient = binance_client
    temporary_history_client: BinanceFuturesClient | None = None
    if isinstance(binance_client, PaperBinanceClient) and settings.binance_api_key and settings.binance_api_secret:
        temporary_history_client = BinanceFuturesClient(settings)
        history_client = temporary_history_client
    try:
        binance_trades = await history_client.user_trades(start_ms, end_ms, limit=1000)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Binance trade history unavailable: %s", str(exc)[:160])
        binance_trades = []
    try:
        funding_rows = await history_client.funding_income(start_ms, end_ms, limit=1000)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Binance funding history unavailable: %s", str(exc)[:160])
        funding_rows = []
    if temporary_history_client is not None:
        await temporary_history_client.stop()
    event_rows = storage.get_events(start_ms, end_ms)
    return TradeHistoryResponse(
        source="币安真实成交/资金费 + MT4 EA 上传的账户历史",
        items=_build_trade_history(mt4_orders, binance_trades, funding_rows, event_rows),
    )


def _build_trade_history(
    mt4_orders: list[Mt4ClosedOrder],
    binance_trades: list[dict],
    funding_rows: list[dict] | None = None,
    event_rows: list[dict[str, Any]] | None = None,
) -> list[TradeHistoryItem]:
    items: list[TradeHistoryItem] = []
    combined_binance_trades = _combined_binance_order_trades(binance_trades)
    exit_allocations = _allocate_exit_trades(mt4_orders, combined_binance_trades)
    for mt4_order in mt4_orders:
        quantity_oz = mt4_order.lots * settings.mt4_lot_size_oz
        entry_side = Side.SELL if mt4_order.side == Side.BUY else Side.BUY
        exit_side = Side.BUY if entry_side == Side.SELL else Side.SELL
        entry_trade = _match_binance_trade(combined_binance_trades, entry_side, quantity_oz, mt4_order.open_time_ms)
        exit_trade = exit_allocations.get(mt4_order.ticket) or _match_binance_trade(combined_binance_trades, exit_side, quantity_oz, mt4_order.close_time_ms)
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
                strategy_version=_trade_history_version(mt4_order.open_time_ms, mt4_order.close_time_ms),
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
    grouped_items = _group_trade_history_items(items)
    event_aligned_items = _align_event_linked_trade_history_items(
        grouped_items,
        combined_binance_trades,
        _build_event_exit_links(event_rows or []),
    )
    aligned_items = _align_unmatched_trade_history_items(event_aligned_items, combined_binance_trades)
    funded_items = _apply_funding_income(aligned_items, funding_rows or [])
    return _apply_trade_history_summaries(funded_items)


def _trade_history_version(open_time_ms: int | None, close_time_ms: int | None) -> str:
    cutoff = settings.gold_v2_history_start_ms
    if cutoff <= 0:
        return "v1.0"
    trade_time = open_time_ms or close_time_ms or 0
    return "v2.0" if trade_time >= cutoff else "v1.0"


def _build_event_exit_links(events: list[dict[str, Any]]) -> dict[frozenset[int], dict[str, Any]]:
    links: dict[frozenset[int], dict[str, Any]] = {}
    latest_exit_order_id: str | None = None
    latest_exit_ms: int | None = None
    for event in sorted(events, key=lambda row: int(row.get("id") or 0)):
        kind = str(event.get("kind") or "")
        payload = event.get("payload") or {}
        event_ms = _event_time_ms(event)
        if kind in {"exit_order", "exit_order_filled", "exit_cancel_race_filled_following_mt4"}:
            order_id = payload.get("order_id")
            if order_id:
                latest_exit_order_id = str(order_id)
                latest_exit_ms = _int_field(payload, "timestamp_ms") or event_ms
        if kind != "open_pair_live_mismatch_paused":
            continue
        if Decimal(str(payload.get("binance_position_qty") or "0")) != 0:
            continue
        positions = payload.get("mt4_positions") or []
        tickets = frozenset(int(position["ticket"]) for position in positions if position.get("ticket") is not None)
        if not tickets or not latest_exit_order_id or latest_exit_ms is None or event_ms is None:
            continue
        if event_ms - latest_exit_ms > HISTORY_EVENT_EXIT_LINK_WINDOW_MS:
            continue
        links.setdefault(
            tickets,
            {
                "exit_order_id": latest_exit_order_id,
                "pair_id": payload.get("pair_id"),
                "linked_ms": event_ms,
            },
        )
    return links


def _event_time_ms(event: dict[str, Any]) -> int | None:
    payload = event.get("payload") or {}
    payload_ms = _int_field(payload, "timestamp_ms")
    if payload_ms is not None:
        return payload_ms
    ts = event.get("ts")
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _align_event_linked_trade_history_items(
    items: list[TradeHistoryItem],
    trades: list[dict],
    links: dict[frozenset[int], dict[str, Any]],
) -> list[TradeHistoryItem]:
    if not links or not trades:
        return items
    trades_by_order_id = {str(trade.get("orderId") or trade.get("order_id")): trade for trade in trades}
    aligned: list[TradeHistoryItem] = []
    for item in items:
        tickets = frozenset(item.mt4_tickets or ([] if item.mt4_ticket is None else [item.mt4_ticket]))
        link = links.get(tickets)
        if not link or item.open_time_ms is None or item.close_time_ms is None or item.mt4_side is None:
            aligned.append(item)
            continue
        final_exit_trade = trades_by_order_id.get(str(link.get("exit_order_id") or ""))
        if not final_exit_trade:
            aligned.append(item)
            continue
        entry_side = Side.SELL if item.mt4_side == Side.BUY else Side.BUY
        exit_side = Side.BUY if entry_side == Side.SELL else Side.SELL
        if str(final_exit_trade.get("side")) != exit_side.value:
            aligned.append(item)
            continue
        realized_trades = _realized_binance_trades_between(trades, exit_side, item.open_time_ms, item.close_time_ms)
        final_order_id = str(final_exit_trade.get("orderId") or final_exit_trade.get("order_id") or "")
        if final_order_id and all(str(trade.get("orderId") or trade.get("order_id") or "") != final_order_id for trade in realized_trades):
            realized_trades.append(final_exit_trade)
        if not realized_trades:
            aligned.append(item)
            continue
        binance_realized = sum((_decimal_field(trade, "realizedPnl") or Decimal("0") for trade in realized_trades), Decimal("0"))
        exit_commission = sum((_decimal_field(trade, "commission") or Decimal("0") for trade in realized_trades), Decimal("0"))
        binance_commission = (item.binance_commission or Decimal("0")) + exit_commission
        mt4_total = (item.mt4_profit or Decimal("0")) + (item.mt4_swap or Decimal("0")) + (item.mt4_commission or Decimal("0"))
        order_ids = _join_unique([str(trade.get("orderId") or trade.get("order_id") or "") for trade in realized_trades])
        ticket_count = len(tickets)
        suffix = f"（{ticket_count}张合并）" if ticket_count > 1 else ""
        aligned.append(
            item.model_copy(
                update={
                    "binance_exit_order_id": order_ids,
                    "binance_exit_side": exit_side,
                    "binance_exit_price": _decimal_field(final_exit_trade, "price"),
                    "binance_realized_pnl": binance_realized,
                    "binance_commission": binance_commission,
                    "net_pnl": binance_realized - binance_commission + mt4_total,
                    "status": f"按事件链对齐真实盈亏，含币安补回{suffix}",
                }
            )
        )
    aligned.sort(key=lambda row: row.close_time_ms or 0, reverse=True)
    return aligned


def _realized_binance_trades_between(trades: list[dict], side: Side, start_ms: int, end_ms: int) -> list[dict]:
    rows = []
    for trade in trades:
        if str(trade.get("side")) != side.value:
            continue
        trade_time = _int_field(trade, "time")
        if trade_time is None or trade_time < start_ms - 60_000 or trade_time > end_ms + 60_000:
            continue
        realized = _decimal_field(trade, "realizedPnl") or Decimal("0")
        if realized == 0:
            continue
        rows.append(trade)
    rows.sort(key=lambda row: _int_field(row, "time") or 0)
    return rows


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


def _apply_trade_history_summaries(items: list[TradeHistoryItem]) -> list[TradeHistoryItem]:
    return [item.model_copy(update={"status": _trade_history_status_with_summary(item)}) for item in items]


def _trade_history_status_with_summary(item: TradeHistoryItem) -> str:
    base_status = item.status or "历史数据"
    if "原因：" in base_status:
        return base_status
    summary = _trade_history_pnl_summary(item)
    return f"{base_status}；{summary}" if summary else base_status


def _trade_history_pnl_summary(item: TradeHistoryItem) -> str | None:
    if item.net_pnl is None:
        return _trade_history_incomplete_summary(item)
    net = item.net_pnl
    outcome = "盈利" if net > 0 else "亏损" if net < 0 else "持平"
    components = _trade_history_components(item)
    positives = [(name, value) for name, value in components if value > 0]
    negatives = [(name, value) for name, value in components if value < 0]
    reasons = positives if net >= 0 else negatives
    if not reasons:
        reasons = positives or negatives
    reason_text = "，".join(f"{name}{_fmt_signed_decimal(value)}" for name, value in sorted(reasons, key=lambda part: abs(part[1]), reverse=True))
    if not reason_text:
        reason_text = "各项收支基本抵消"
    offset_text = _trade_history_offset_text(net, positives, negatives)
    notes = _trade_history_quality_notes(item)
    details = f"原因：本单{outcome}{_fmt_signed_decimal(net)}，主要来自{reason_text}{offset_text}"
    if notes:
        details += f"；{notes}"
    return details


def _trade_history_incomplete_summary(item: TradeHistoryItem) -> str | None:
    missing = []
    if item.binance_realized_pnl is None:
        missing.append("币安实际盈亏")
    if item.mt4_profit is None:
        missing.append("MT4实际盈亏")
    if not missing:
        return None
    return f"原因：暂不能判断盈亏，缺少{'、'.join(missing)}"


def _trade_history_components(item: TradeHistoryItem) -> list[tuple[str, Decimal]]:
    mt4_total = (item.mt4_profit or Decimal("0")) + (item.mt4_swap or Decimal("0")) + (item.mt4_commission or Decimal("0"))
    components = [
        ("币安合约盈亏", item.binance_realized_pnl or Decimal("0")),
        ("MT4盈亏", mt4_total),
        ("币安资金费", item.binance_funding_income or Decimal("0")),
    ]
    if item.binance_commission is not None:
        components.append(("币安手续费", -abs(item.binance_commission)))
    return components


def _trade_history_offset_text(
    net: Decimal,
    positives: list[tuple[str, Decimal]],
    negatives: list[tuple[str, Decimal]],
) -> str:
    offsets = negatives if net >= 0 else positives
    if not offsets:
        return ""
    offset_text = "，".join(f"{name}{_fmt_signed_decimal(value)}" for name, value in sorted(offsets, key=lambda part: abs(part[1]), reverse=True)[:2])
    return f"，被{offset_text}抵消一部分"


def _trade_history_quality_notes(item: TradeHistoryItem) -> str:
    notes = []
    if "数量不一致" in (item.status or ""):
        notes.append("币安和 MT4 数量不一致，净利按能对齐到的真实成交计算")
    if item.binance_entry_order_id is None:
        notes.append("币安开仓成交未完全匹配")
    if item.binance_exit_order_id is None:
        notes.append("币安平仓成交未完全匹配")
    return "；".join(notes)


def _fmt_signed_decimal(value: Decimal) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}{_fmt_decimal(abs(value))}U"


def _group_trade_history_items(items: list[TradeHistoryItem]) -> list[TradeHistoryItem]:
    grouped: dict[str, list[TradeHistoryItem]] = {}
    unmatched: list[TradeHistoryItem] = []
    for item in items:
        if item.binance_exit_order_id:
            key = f"{item.strategy_version}:{item.binance_exit_order_id}"
            grouped.setdefault(key, []).append(item)
        else:
            unmatched.append(item)
    rows = [_merge_trade_group(group) for group in grouped.values()]
    rows.extend(_merge_trade_group(group) for group in _cluster_mt4_history_batches(unmatched))
    rows.sort(key=lambda item: item.close_time_ms or 0, reverse=True)
    return rows


def _cluster_mt4_history_batches(items: list[TradeHistoryItem]) -> list[list[TradeHistoryItem]]:
    batches: list[list[TradeHistoryItem]] = []
    sorted_items = sorted(items, key=lambda item: (item.mt4_side.value if item.mt4_side else "", item.close_time_ms or 0))
    for item in sorted_items:
        last_batch = batches[-1] if batches else []
        last_item = last_batch[-1] if last_batch else None
        same_side = bool(last_item and last_item.mt4_side == item.mt4_side)
        same_version = bool(last_item and last_item.strategy_version == item.strategy_version)
        last_close = last_item.close_time_ms if last_item else None
        current_close = item.close_time_ms
        near_close = last_close is not None and current_close is not None and abs(current_close - last_close) <= HISTORY_MT4_BATCH_WINDOW_MS
        if same_side and same_version and near_close:
            last_batch.append(item)
        else:
            batches.append([item])
    return batches


def _align_unmatched_trade_history_items(items: list[TradeHistoryItem], trades: list[dict]) -> list[TradeHistoryItem]:
    if not trades:
        return items
    used_exit_order_ids = {
        order_id
        for item in items
        for order_id in _split_order_ids(item.binance_exit_order_id)
        if order_id
    }
    aligned: list[TradeHistoryItem] = []
    for item in sorted(items, key=lambda row: row.close_time_ms or 0):
        if item.net_pnl is not None or not item.mt4_side or item.close_time_ms is None:
            aligned.append(item)
            continue
        quantity_oz = item.quantity_oz or Decimal("0")
        entry_side = Side.SELL if item.mt4_side == Side.BUY else Side.BUY
        exit_side = Side.BUY if entry_side == Side.SELL else Side.SELL
        exit_trade = _match_binance_trade_loose(
            trades,
            exit_side,
            item.close_time_ms,
            used_order_ids=used_exit_order_ids,
            preferred_quantity=quantity_oz,
            prefer_realized=True,
        )
        if not exit_trade:
            aligned.append(item)
            continue
        exit_order_id = str(exit_trade.get("orderId") or exit_trade.get("order_id") or "")
        if exit_order_id:
            used_exit_order_ids.add(exit_order_id)
        entry_trade = None
        if item.open_time_ms is not None:
            entry_trade = _match_binance_trade_loose(
                trades,
                entry_side,
                item.open_time_ms,
                used_order_ids=set(),
                preferred_quantity=quantity_oz,
                prefer_realized=False,
            )
        entry_commission = (_decimal_field(entry_trade, "commission") if entry_trade else None) or Decimal("0")
        exit_commission = (_decimal_field(exit_trade, "commission") if exit_trade else None) or Decimal("0")
        binance_commission = entry_commission + exit_commission
        binance_realized = _decimal_field(exit_trade, "realizedPnl")
        mt4_total = (item.mt4_profit or Decimal("0")) + (item.mt4_swap or Decimal("0")) + (item.mt4_commission or Decimal("0"))
        net = binance_realized - binance_commission + mt4_total if binance_realized is not None else None
        aligned.append(
            item.model_copy(
                update={
                    "binance_entry_order_id": item.binance_entry_order_id or (str(entry_trade.get("orderId")) if entry_trade else None),
                    "binance_entry_side": item.binance_entry_side or (entry_side if entry_trade else None),
                    "binance_entry_price": item.binance_entry_price or (_decimal_field(entry_trade, "price") if entry_trade else None),
                    "binance_exit_order_id": str(exit_trade.get("orderId")) if exit_trade else None,
                    "binance_exit_side": exit_side,
                    "binance_exit_price": _decimal_field(exit_trade, "price"),
                    "binance_realized_pnl": binance_realized,
                    "binance_commission": binance_commission,
                    "net_pnl": net,
                    "status": _aligned_history_status(item, entry_trade, exit_trade),
                }
            )
        )
    aligned.sort(key=lambda row: row.close_time_ms or 0, reverse=True)
    return aligned


def _aligned_history_status(item: TradeHistoryItem, entry_trade: dict | None, exit_trade: dict) -> str:
    ticket_count = len(item.mt4_tickets or ([] if item.mt4_ticket is None else [item.mt4_ticket]))
    suffix = f"（{ticket_count}张合并）" if ticket_count > 1 else ""
    binance_qty = _decimal_field(exit_trade, "qty")
    mt4_qty = item.quantity_oz or Decimal("0")
    if binance_qty is not None and abs(binance_qty - mt4_qty) > QTY_EPSILON:
        return f"数量不一致，按时间对齐真实盈亏（币安{_fmt_decimal(binance_qty)} XAU / MT4 {_fmt_decimal(mt4_qty)} XAU）{suffix}"
    if entry_trade:
        return f"按时间对齐真实盈亏{suffix}"
    return f"按时间对齐真实平仓，开仓成交未完全匹配{suffix}"


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
    has_exit_net = all(item.binance_exit_order_id and item.net_pnl is not None for item in group)
    if complete:
        status = f"完整真实数据（{len(group)}张合并）"
    elif has_exit_net:
        status = f"真实平仓数据，开仓成交未完全匹配（{len(group)}张合并）"
    else:
        status = f"部分缺少币安成交匹配（{len(group)}张合并）"
    return TradeHistoryItem(
        strategy_version=_same_value([item.strategy_version for item in group]) or "混合版本",
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
        status=status,
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


def _split_order_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split("/") if part.strip()]


def _fmt_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f")


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
            if used_qty + quantity_oz > trade_qty + QTY_EPSILON:
                continue
            selected.append((order, quantity_oz))
            used_qty += quantity_oz
            if abs(used_qty - trade_qty) <= QTY_EPSILON:
                break
        if not selected or abs(used_qty - trade_qty) > QTY_EPSILON:
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
        if abs(qty - quantity) <= QTY_EPSILON:
            candidates.append((distance, trade))
    if not candidates:
        grouped_candidates = []
        for parts in grouped.values():
            total_qty = sum((_decimal_field(part, "qty") or Decimal("0") for part in parts), Decimal("0"))
            if abs(total_qty - quantity) > QTY_EPSILON:
                continue
            closest = min(abs((_int_field(part, "time") or target_time_ms) - target_time_ms) for part in parts)
            grouped_candidates.append((closest, _combine_trade_parts(parts)))
        if not grouped_candidates:
            return None
        grouped_candidates.sort(key=lambda item: item[0])
        return grouped_candidates[0][1]
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _combined_binance_order_trades(trades: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str], list[dict]] = {}
    passthrough: list[dict] = []
    for trade in trades:
        order_id = str(trade.get("orderId") or trade.get("order_id") or "")
        side = str(trade.get("side") or "")
        if not order_id or not side:
            passthrough.append(trade)
            continue
        grouped.setdefault((order_id, side), []).append(trade)
    combined = [_combine_trade_parts(parts) for parts in grouped.values()]
    combined.extend(passthrough)
    return combined


def _match_binance_trade_loose(
    trades: list[dict],
    side: Side,
    target_time_ms: int,
    used_order_ids: set[str],
    preferred_quantity: Decimal | None = None,
    prefer_realized: bool = False,
) -> dict | None:
    candidates = []
    for trade in trades:
        if str(trade.get("side")) != side.value:
            continue
        order_id = str(trade.get("orderId") or trade.get("order_id") or "")
        if order_id and order_id in used_order_ids:
            continue
        trade_time = _int_field(trade, "time")
        if trade_time is None:
            continue
        distance = abs(trade_time - target_time_ms)
        if distance > HISTORY_BINANCE_ALIGN_WINDOW_MS:
            continue
        qty = _decimal_field(trade, "qty") or Decimal("0")
        quantity_distance = abs(qty - preferred_quantity) if preferred_quantity is not None else Decimal("0")
        realized = _decimal_field(trade, "realizedPnl") or Decimal("0")
        realized_rank = 0 if (not prefer_realized or realized != 0) else 1
        candidates.append((realized_rank, quantity_distance, distance, trade))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return candidates[0][3]


def _combine_trade_parts(parts: list[dict]) -> dict:
    first = dict(parts[0])
    total_qty = sum((_decimal_field(part, "qty") or Decimal("0") for part in parts), Decimal("0"))
    if total_qty <= 0:
        return first
    notional = sum(((_decimal_field(part, "price") or Decimal("0")) * (_decimal_field(part, "qty") or Decimal("0")) for part in parts), Decimal("0"))
    realized = sum((_decimal_field(part, "realizedPnl") or Decimal("0") for part in parts), Decimal("0"))
    commission = sum((_decimal_field(part, "commission") or Decimal("0") for part in parts), Decimal("0"))
    times = [_int_field(part, "time") for part in parts]
    first["qty"] = str(total_qty)
    first["price"] = str(notional / total_qty)
    first["realizedPnl"] = str(realized)
    first["commission"] = str(commission)
    first["time"] = max(time for time in times if time is not None) if any(time is not None for time in times) else first.get("time")
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
    if settings.gold_v2_observation_only:
        raise HTTPException(status_code=400, detail="新版七步观察阶段已锁定只读，暂不允许启动实盘")
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
            if settings.gold_v2_observation_only:
                strategy.last_error = None
            elif _live_pair_operation_cooldown_until_ms > _now_ms():
                remaining_ms = _live_pair_operation_cooldown_until_ms - _now_ms()
                if strategy.open_pair is not None and strategy.state == StrategyState.PAIR_OPEN:
                    strategy.last_error = f"币安接口临时限频冷却中，约 {max(1, remaining_ms // 1000)} 秒后重新对账"
            elif not await _reconcile_open_pair_live_state():
                await strategy.step()
        except Exception as exc:  # noqa: BLE001
            strategy.last_error = str(exc)[:240]
            logger.exception("strategy loop error")
        _persist_runtime_state()
        await asyncio.sleep(settings.loop_interval_ms / 1000)


async def _reconcile_open_pair_live_state() -> bool:
    global _live_pair_reconcile_ms, _live_pair_reconcile_error_count, _live_pair_operation_cooldown_until_ms
    if settings.is_dry_run or strategy.open_pair is None:
        return False
    if strategy.active_order is not None or strategy.state not in {StrategyState.PAIR_OPEN, StrategyState.PAUSED}:
        return False
    if not mt4_bridge.connected():
        return False
    now = _now_ms()
    if now - _live_pair_reconcile_ms < LIVE_PAIR_RECONCILE_INTERVAL_MS:
        return False
    _live_pair_reconcile_ms = now
    try:
        binance_qty = await _binance_position_quantity(force=True)
        if binance_qty is None:
            raise RuntimeError("Binance position cache unavailable")
    except Exception as exc:  # noqa: BLE001
        _live_pair_reconcile_error_count += 1
        error_text = str(exc)[:160]
        storage.record_event(
            "open_pair_live_reconcile_failed",
            {"error": error_text, "count": _live_pair_reconcile_error_count},
        )
        if is_transient_live_reconcile_error(error_text):
            cooldown_ms = _binance_transient_cooldown_ms(error_text)
            _live_pair_operation_cooldown_until_ms = max(
                _live_pair_operation_cooldown_until_ms,
                _now_ms() + cooldown_ms,
            )
            if _paused_for_transient_reconcile():
                strategy.state = StrategyState.PAIR_OPEN
            strategy.last_error = f"币安接口临时限频，已冷却约 {cooldown_ms // 1000} 秒后再对账；冷却期间不发起新挂单/撤单"
            storage.record_event(
                "open_pair_live_reconcile_transient_cooldown",
                {"error": error_text, "cooldown_ms": cooldown_ms},
            )
            return True
        strategy.state = StrategyState.PAUSED
        strategy.last_error = f"组合实盘对账失败，已暂停：{error_text}"
        return True
    _live_pair_reconcile_error_count = 0
    mt4_positions = mt4_bridge.positions()
    action = open_pair_live_reconcile_action(
        strategy.open_pair,
        binance_qty,
        mt4_positions,
        settings.mt4_symbol,
        settings.mt4_lot_size_oz,
    )
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
        mt4_symbol_positions = [position for position in mt4_positions if position.symbol == settings.mt4_symbol]
        if binance_qty == 0 and mt4_symbol_positions:
            if strategy.queue_mt4_close_after_binance_flat_mismatch(mt4_symbol_positions):
                storage.record_event(
                    "open_pair_live_mismatch_auto_mt4_close",
                    {
                        "pair_id": strategy.open_pair.pair_id if strategy.open_pair else None,
                        "binance_position_qty": str(binance_qty),
                        "mt4_positions": [
                            {"ticket": position.ticket, "symbol": position.symbol, "side": position.side.value, "lots": str(position.lots)}
                            for position in mt4_symbol_positions
                        ],
                    },
                )
                _persist_runtime_state()
                return True
        strategy.state = StrategyState.PAUSED
        strategy.last_error = "组合持仓与实盘不一致：币安或 MT4 方向/数量不匹配，已暂停，请人工确认"
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
    if strategy.state == StrategyState.PAUSED and strategy.last_error and strategy.last_error.startswith("组合持仓与实盘不一致"):
        strategy.state = StrategyState.PAIR_OPEN
        strategy.last_error = None
        storage.record_event(
            "open_pair_live_mismatch_recovered",
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
    if _paused_for_transient_reconcile():
        strategy.state = StrategyState.PAIR_OPEN
        strategy.last_error = None
        storage.record_event(
            "open_pair_live_transient_reconcile_recovered",
            {
                "pair_id": strategy.open_pair.pair_id,
                "binance_position_qty": str(binance_qty),
            },
        )
        return True
    return False


def _paused_for_transient_reconcile() -> bool:
    return (
        strategy.state == StrategyState.PAUSED
        and bool(strategy.last_error)
        and strategy.last_error.startswith("组合实盘对账失败")
        and is_transient_live_reconcile_error(strategy.last_error)
    )


def _binance_transient_cooldown_ms(error_text: str) -> int:
    match = re.search(r"banned until (\d{13})", error_text)
    if match:
        wait_ms = int(match.group(1)) - utc_now_ms() + 5_000
        return max(30_000, min(wait_ms, 300_000))
    return 60_000


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
            if _paused_for_transient_reconcile():
                strategy.state = StrategyState.PAIR_OPEN
                strategy.last_error = "上次因币安接口临时限频暂停，已恢复组合持仓监控并等待重新对账"
                storage.record_event(
                    "runtime_state_transient_pause_resumed",
                    {"pair_id": strategy.open_pair.pair_id},
                )
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
        gold_v2_observation_only=settings.gold_v2_observation_only,
        gold_v2_history_start_ms=settings.gold_v2_history_start_ms,
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
        post_exit_reentry_cooldown_ms=settings.post_exit_reentry_cooldown_ms,
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
        mt4_min_lot=settings.mt4_min_lot,
        mt4_lot_step=settings.mt4_lot_step,
        mt4_slippage_points=settings.mt4_slippage_points,
        mt4_close_extra_buffer_usd=settings.mt4_close_extra_buffer_usd,
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
    binance_entry_price = _effective_binance_entry_price(pair, binance_snapshot)
    mt4_entry_price, mt4_lots = _mt4_average_entry_price(pair)
    funding_estimate = _estimate_binance_funding(pair, qty, funding, binance_quote)
    mt4_swap_estimate = _estimate_mt4_swap(pair, qty, swap_info)
    accrued_funding = await _binance_accrued_funding(pair)
    accrued_swap = _mt4_accrued_swap(pair)
    gross = _estimate_close_gross(pair, binance_quote, mt4_quote, binance_entry_price, mt4_entry_price)
    fees = _estimate_binance_fees(pair, binance_quote, binance_entry_price, include_entry_fee=True)
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
    global _binance_accrued_funding_failure_ms
    if settings.is_dry_run:
        return None
    now = _now_ms()
    if (
        _binance_accrued_funding_cache_pair_id == pair.pair_id
        and _binance_accrued_funding_cache_value is not None
        and now - _binance_accrued_funding_cache_ms <= FUNDING_INCOME_CACHE_TTL_MS
    ):
        return _binance_accrued_funding_cache_value
    if now - _binance_accrued_funding_failure_ms <= FUNDING_INCOME_FAILURE_RETRY_MS:
        if _binance_accrued_funding_cache_pair_id == pair.pair_id:
            return _binance_accrued_funding_cache_value
        return None
    try:
        rows = await binance_client.funding_income(pair.opened_ms - 60_000, utc_now_ms(), limit=1000)
        total = sum((_decimal_field(row, "income") or Decimal("0") for row in rows), Decimal("0"))
        _binance_accrued_funding_cache_pair_id = pair.pair_id
        _binance_accrued_funding_cache_value = total
        _binance_accrued_funding_cache_ms = now
        _binance_accrued_funding_failure_ms = 0
        return total
    except Exception as exc:  # noqa: BLE001
        _binance_accrued_funding_failure_ms = now
        logger.warning("Binance funding income unavailable: %s", str(exc)[:160])
        if _binance_accrued_funding_cache_pair_id == pair.pair_id:
            return _binance_accrued_funding_cache_value
        return None


def _effective_binance_entry_price(pair, snapshot: BinancePositionSnapshot | None) -> Decimal:
    if snapshot and snapshot.position_amt != 0:
        if snapshot.entry_price is not None:
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
    adjustment = pair.realized_pnl + (accrued_funding or Decimal("0")) + (accrued_swap or Decimal("0")) - fees
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
        return max(settings.close_profit_usd_per_oz, settings.aged_close_profit_usd_per_oz)
    return settings.close_profit_usd_per_oz


def _exit_follow_buffer_usd_per_oz(swap_info) -> Decimal:
    point = swap_info.point or Decimal("0.01")
    return (Decimal(settings.mt4_slippage_points) * point) + settings.mt4_close_extra_buffer_usd


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
        return pair.realized_pnl + (binance_entry - binance_exit) * qty + (mt4_exit - mt4_entry) * qty
    binance_exit = round_up(binance_quote.ask, binance_client.filters.tick_size)
    mt4_exit = mt4_quote.ask
    return pair.realized_pnl + (binance_exit - binance_entry) * qty + (mt4_entry - mt4_exit) * qty


def _estimate_binance_fees(
    pair,
    binance_quote: MarketQuote | None,
    binance_entry_price: Decimal | None = None,
    include_entry_fee: bool = True,
) -> Decimal | None:
    fee_rate = binance_client.maker_fee_rate
    if fee_rate is None or not binance_quote:
        return None
    qty = pair.quantity_oz
    entry_price = binance_entry_price or pair.binance_entry_price
    if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
        exit_price = round_down(binance_quote.bid, binance_client.filters.tick_size)
    else:
        exit_price = round_up(binance_quote.ask, binance_client.filters.tick_size)
    notional = exit_price * qty
    if include_entry_fee:
        notional += entry_price * qty
    return notional * abs(fee_rate)


def _execution_plan(metrics: PositionMetrics | None = None) -> ExecutionPlanStatus:
    max_follow_seconds = Decimal(settings.max_hedge_delay_ms) / Decimal("1000")
    order = strategy.active_order
    plan = strategy.active_plan
    if order:
        follow_side = plan.mt4_hedge_side if plan else None
        mt4_price_limit = plan.mt4_price_limit if plan else None
        if strategy.state == StrategyState.REPAIRING_BINANCE_EXIT:
            summary = f"币安平仓部分成交后正在用 Post Only 补回：{_side_text(order.side)} {order.orig_qty} XAU，价格 {order.price}，状态 {_order_status_text(order.status.value)}；补回成交前不会平 MT4，不走币安市价。"
            follow_side = None
            mt4_price_limit = None
        elif order.reduce_only:
            summary = f"币安当前平仓挂单：{_side_text(order.side)} {order.orig_qty} XAU，价格 {order.price}，状态 {_order_status_text(order.status.value)}；币安全部成交后 MT4 逐张市价平仓。"
            follow_side = Side.SELL if strategy.open_pair and strategy.open_pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG else Side.BUY
            mt4_price_limit = None
        else:
            limit_text = f"，MT4 保护价 {_mt4_limit_text(follow_side, mt4_price_limit)}" if follow_side and mt4_price_limit is not None else ""
            summary = f"币安当前挂单：{_side_text(order.side)} {order.orig_qty} XAU，价格 {order.price}，状态 {_order_status_text(order.status.value)}；成交后 MT4 立即市价对冲{limit_text}。"
        return ExecutionPlanStatus(
            summary=summary,
            active_binance_order=True,
            binance_order_status=order.status,
            binance_order_side=order.side,
            binance_order_price=order.price,
            binance_order_qty=order.orig_qty,
            binance_order_executed_qty=order.executed_qty,
            mt4_follow_side=follow_side,
            mt4_price_limit=mt4_price_limit,
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
                summary=f"补仓条件已满足：{add_summary}；币安将同向挂 {_side_text(add_plan.binance_side)} 限价 {add_plan.limit_price}，数量 {add_plan.quantity_oz} XAU；成交后 MT4 同向 {_side_text(add_plan.mt4_hedge_side)} 市价跟随，保护价 {_mt4_limit_text(add_plan.mt4_hedge_side, add_plan.mt4_price_limit)}。",
                binance_order_side=add_plan.binance_side,
                binance_order_price=add_plan.limit_price,
                binance_order_qty=add_plan.quantity_oz,
                mt4_follow_side=add_plan.mt4_hedge_side,
                mt4_price_limit=add_plan.mt4_price_limit,
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
        close_order_text = (
            f"平仓条件满足后，币安才会挂 {_side_text(side)} 限价 {price}"
            if not close_ready
            else f"平仓条件已满足，币安准备挂 {_side_text(side)} 限价 {price}"
        )
        return ExecutionPlanStatus(
            summary=f"平仓逻辑：按实盘进场价差计算，保本价差 {break_even_text}，先扣 {close_profit} 美元/盎司利润空间{buffer_text}，当前触发价差 {trigger_text}；{close_order_text}；当前平仓价差 {spread if spread is not None else '-'}，{'已满足' if close_ready else '未满足'}。{add_summary}",
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
                summary=f"开仓条件已满足：{confirm_text}；币安将挂 {_side_text(entry_plan.binance_side)} 限价 {entry_plan.limit_price}，数量 {entry_plan.quantity_oz} XAU；成交后 MT4 {_side_text(entry_plan.mt4_hedge_side)} 立即市价对冲，保护价 {_mt4_limit_text(entry_plan.mt4_hedge_side, entry_plan.mt4_price_limit)}。",
                binance_order_side=entry_plan.binance_side,
                binance_order_price=entry_plan.limit_price,
                binance_order_qty=entry_plan.quantity_oz,
                mt4_follow_side=entry_plan.mt4_hedge_side,
                mt4_price_limit=entry_plan.mt4_price_limit,
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
    trigger_edge = _pair_next_add_trigger_edge(pair, metrics)
    if trigger_edge is None:
        return None
    return build_directional_entry_plan(settings, binance_client.filters, binance_quote, mt4_quote, pair.direction, trigger_edge)


def _pair_add_summary(pair, binance_quote: MarketQuote, mt4_quote: MarketQuote, metrics: PositionMetrics | None = None) -> str:
    if settings.max_add_count <= 0:
        return "补仓已关闭。"
    if pair.add_count >= settings.max_add_count:
        return f"补仓次数 {pair.add_count}/{settings.max_add_count}，已达上限。"
    anchor_edge = _pair_add_anchor_edge(pair, metrics)
    if anchor_edge is None:
        return "补仓基准价差缺失，暂不补仓。"
    trigger_edge = anchor_edge + settings.add_edge_growth_usd
    current_edge = binance_quote.ask - mt4_quote.ask if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG else mt4_quote.bid - binance_quote.bid
    if pair.add_count > 0:
        actual_text = f"，上次实得价差 {pair.last_add_edge:.4f} 美元" if pair.last_add_edge is not None else ""
        return f"补仓观察：已补 {pair.add_count}/{settings.max_add_count} 次，下次补仓基差 {trigger_edge:.4f} 美元（上次触发阶梯 {anchor_edge:.4f} + 增加 {settings.add_edge_growth_usd:.4f}）{actual_text}，当前同向价差 {current_edge:.4f} 美元。"
    return f"补仓观察：已补 {pair.add_count}/{settings.max_add_count} 次，下次补仓基差 {trigger_edge:.4f} 美元（首仓基准 {anchor_edge:.4f} + 增加 {settings.add_edge_growth_usd:.4f}），当前同向价差 {current_edge:.4f} 美元。"


def _mt4_limit_text(side: Side | None, price: Decimal | None) -> str:
    if side == Side.BUY and price is not None:
        return f"最高 {price}"
    if side == Side.SELL and price is not None:
        return f"最低 {price}"
    return "未设置"


def _pair_next_add_trigger_edge(pair, metrics: PositionMetrics | None = None) -> Decimal | None:
    anchor = _pair_add_anchor_edge(pair, metrics)
    if anchor is None:
        return None
    return anchor + settings.add_edge_growth_usd


def _pair_add_anchor_edge(pair, metrics: PositionMetrics | None = None) -> Decimal | None:
    if pair.add_count == 0:
        base = pair.base_edge
        if metrics and metrics.actual_entry_spread is not None:
            return metrics.actual_entry_spread
        mt4_entry, _lots = _mt4_average_entry_price(pair)
        current = _actual_entry_spread(pair, pair.binance_entry_price, mt4_entry or pair.mt4_entry_price)
        return base if current is None else current
    return pair.last_add_trigger_edge or pair.last_add_edge or pair.base_edge


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
