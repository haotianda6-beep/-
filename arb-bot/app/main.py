from __future__ import annotations

import asyncio
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
from app.models import (
    EngineStatus,
    ExecutionPlanStatus,
    MarketQuote,
    Mt4HistoryPayload,
    Mt4Report,
    Mt4Tick,
    OrderStatus,
    PairDirection,
    PositionMetrics,
    RuntimeConfig,
    RuntimeConfigUpdate,
    Side,
    SpreadAnalysis,
)
from app.mt4_bridge import Mt4Bridge
from app.risk import RiskManager
from app.storage import Storage
from app.strategy import StrategyEngine, build_entry_plan, round_down, round_up


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
WEB_DIR = Path(__file__).resolve().parents[1] / "web"
MT4_DIR = Path(__file__).resolve().parents[1] / "mt4"


@app.on_event("startup")
async def startup() -> None:
    global _loop_task
    await binance_client.start()
    mode = "dry-run" if settings.is_dry_run else "live"
    logger.info("arb executor starting mode=%s symbol=%s/%s", mode, settings.binance_symbol, settings.mt4_symbol)
    _loop_task = asyncio.create_task(_strategy_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    if _loop_task:
        _loop_task.cancel()
    await _cancel_unfilled_active_order_on_shutdown()
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
    if order.executed_qty > 0:
        logger.warning("active Binance order has fills during shutdown order_id=%s executed_qty=%s", order.order_id, order.executed_qty)
        return
    if order.status in {OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED}:
        return
    try:
        await binance_client.cancel_order(order.order_id)
        logger.info("canceled unfilled active Binance order during shutdown order_id=%s", order.order_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to cancel active Binance order during shutdown: %s", str(exc)[:160])


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
        binance_quote=binance_client.latest_quote(),
        mt4_quote=mt4_bridge.latest_quote(),
        open_pair=strategy.open_pair,
        position_metrics=_position_metrics(),
        execution_plan=_execution_plan(),
        last_error=strategy.last_error,
        config=_runtime_config(),
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
    return {"status": "ok", "symbol": quote.symbol, "timestamp_ms": quote.timestamp_ms}


@app.post("/mt4/history")
async def mt4_history(payload: Mt4HistoryPayload, x_mt4_token: str | None = Header(default=None)) -> dict:
    if not mt4_bridge.token_ok(x_mt4_token or payload.token):
        raise HTTPException(status_code=403, detail="invalid MT4 token")
    if payload.symbol != settings.mt4_symbol:
        raise HTTPException(status_code=400, detail="MT4 品种不匹配")
    saved = storage.upsert_bars("mt4", payload.symbol, payload.interval, payload.bars)
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
    strategy.resume()
    return {"status": "ok", "state": strategy.state}


@app.post("/control/paper/clear")
async def clear_paper_state() -> dict:
    if not settings.is_dry_run:
        raise HTTPException(status_code=400, detail="实盘模式不允许清理运行持仓状态")
    if isinstance(binance_client, PaperBinanceClient):
        binance_client.clear_orders()
    strategy.clear_runtime_state()
    return {"status": "ok", "state": strategy.state}


@app.post("/control/live/start")
async def start_live_mode() -> dict:
    _assert_live_preflight()
    if isinstance(binance_client, PaperBinanceClient):
        binance_client.clear_orders()
    strategy.clear_runtime_state()
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
            await strategy.step()
        except Exception as exc:  # noqa: BLE001
            strategy.last_error = str(exc)[:240]
            logger.exception("strategy loop error")
        await asyncio.sleep(settings.loop_interval_ms / 1000)


def _runtime_config() -> RuntimeConfig:
    return RuntimeConfig(
        binance_api_configured=bool(settings.binance_api_key and settings.binance_api_secret),
        config_files=[str(path) for path in existing_env_paths()],
        mt4_script_path=str((MT4_DIR / "ArbBridgeEA.mq4").resolve()),
        binance_leverage=settings.binance_leverage,
        binance_entry_offset_usd=settings.binance_entry_offset_usd,
        open_min_edge=settings.open_min_edge,
        close_max_spread=settings.close_max_spread,
        min_locked_edge=settings.min_locked_edge,
        max_order_age_ms=settings.max_order_age_ms,
        max_quote_age_ms=settings.max_quote_age_ms,
        max_hedge_delay_ms=settings.max_hedge_delay_ms,
        max_unhedged_loss_usd_per_oz=settings.max_unhedged_loss_usd_per_oz,
        daily_loss_limit_usdt=settings.daily_loss_limit_usdt,
        target_oz=settings.target_oz,
        mt4_lot_size_oz=settings.mt4_lot_size_oz,
        mt4_slippage_points=settings.mt4_slippage_points,
        loop_interval_ms=settings.loop_interval_ms,
        paper_auto_fill=settings.paper_auto_fill,
        paper_fill_delay_ms=settings.paper_fill_delay_ms,
    )


def _position_metrics() -> PositionMetrics:
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
    funding_estimate = _estimate_binance_funding(pair, qty, funding, binance_quote)
    mt4_swap_estimate = _estimate_mt4_swap(pair, qty, swap_info)
    accrued_swap = _mt4_accrued_swap(pair)
    gross = _estimate_close_gross(pair, binance_quote, mt4_quote)
    fees = _estimate_binance_fees(pair, binance_quote)
    net = None
    if gross is not None and fees is not None:
        net = gross - fees
        if funding_estimate is not None:
            net += funding_estimate
        if mt4_swap_estimate is not None:
            net += mt4_swap_estimate
        if accrued_swap is not None:
            net += accrued_swap
    return metrics.model_copy(
        update={
            "binance_funding_estimate": funding_estimate,
            "mt4_swap_estimate": mt4_swap_estimate,
            "mt4_accrued_swap": accrued_swap,
            "estimated_close_gross": gross,
            "estimated_fees": fees,
            "estimated_close_net": net,
        }
    )


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
    positions = mt4_bridge.positions()
    if not positions:
        return None
    if pair.mt4_ticket is not None:
        matched = [position for position in positions if position.ticket == pair.mt4_ticket]
    else:
        expected_side = Side.BUY if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG else Side.SELL
        matched = [position for position in positions if position.symbol == settings.mt4_symbol and position.side == expected_side]
    if not matched:
        return None
    return sum((position.swap for position in matched), Decimal("0"))


def _estimate_close_gross(pair, binance_quote: MarketQuote | None, mt4_quote: MarketQuote | None) -> Decimal | None:
    if not binance_quote or not mt4_quote:
        return None
    qty = pair.quantity_oz
    if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
        binance_exit = round_down(binance_quote.bid, binance_client.filters.tick_size)
        mt4_exit = mt4_quote.bid
        return (pair.binance_entry_price - binance_exit) * qty + (mt4_exit - pair.mt4_entry_price) * qty
    binance_exit = round_up(binance_quote.ask, binance_client.filters.tick_size)
    mt4_exit = mt4_quote.ask
    return (binance_exit - pair.binance_entry_price) * qty + (pair.mt4_entry_price - mt4_exit) * qty


def _estimate_binance_fees(pair, binance_quote: MarketQuote | None) -> Decimal | None:
    fee_rate = binance_client.maker_fee_rate
    if fee_rate is None or not binance_quote:
        return None
    qty = pair.quantity_oz
    if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
        exit_price = round_down(binance_quote.bid, binance_client.filters.tick_size)
    else:
        exit_price = round_up(binance_quote.ask, binance_client.filters.tick_size)
    return (pair.binance_entry_price * qty + exit_price * qty) * abs(fee_rate)


def _execution_plan() -> ExecutionPlanStatus:
    max_follow_seconds = Decimal(settings.max_hedge_delay_ms) / Decimal("1000")
    order = strategy.active_order
    plan = strategy.active_plan
    if order:
        follow_side = plan.mt4_hedge_side if plan else None
        price_limit = plan.mt4_price_limit if plan else None
        return ExecutionPlanStatus(
            summary=f"币安当前挂单：{_side_text(order.side)} {order.orig_qty} XAU，价格 {order.price}，状态 {_order_status_text(order.status.value)}。",
            active_binance_order=True,
            binance_order_status=order.status,
            binance_order_side=order.side,
            binance_order_price=order.price,
            binance_order_qty=order.orig_qty,
            binance_order_executed_qty=order.executed_qty,
            mt4_follow_side=follow_side,
            mt4_price_limit=price_limit,
            max_follow_seconds=max_follow_seconds,
        )

    pair = strategy.open_pair
    binance_quote = binance_client.latest_quote()
    mt4_quote = mt4_bridge.latest_quote()
    quote_block_reason = _quote_plan_block_reason(binance_quote, mt4_quote)
    if pair and binance_quote and mt4_quote:
        if quote_block_reason:
            return ExecutionPlanStatus(summary=f"当前有组合持仓，但{quote_block_reason}，暂不挂平仓单。", max_follow_seconds=max_follow_seconds)
        spread = abs(binance_quote.mid - mt4_quote.mid)
        close_ready = spread <= settings.close_max_spread
        if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
            side = Side.BUY
            price = round_down(binance_quote.bid, binance_client.filters.tick_size)
        else:
            side = Side.SELL
            price = round_up(binance_quote.ask, binance_client.filters.tick_size)
        return ExecutionPlanStatus(
            summary=f"平仓逻辑：两边中间价差小于等于 {settings.close_max_spread} 美元时，币安挂 {_side_text(side)} 限价 {price}；当前价差 {spread:.4f}，{'已满足' if close_ready else '未满足'}。",
            binance_order_side=side,
            binance_order_price=price,
            binance_order_qty=pair.quantity_oz,
            mt4_follow_side=Side.SELL if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG else Side.BUY,
            max_follow_seconds=max_follow_seconds,
        )

    if strategy.state.value == "PAUSED":
        return ExecutionPlanStatus(summary="系统已暂停，不会自动新挂单；恢复后才会继续按价差条件执行。", max_follow_seconds=max_follow_seconds)
    if binance_quote and mt4_quote:
        if quote_block_reason:
            return ExecutionPlanStatus(summary=f"{quote_block_reason}，暂不挂开仓单。", max_follow_seconds=max_follow_seconds)
        entry_plan = build_entry_plan(settings, binance_client.filters, binance_quote, mt4_quote)
        if entry_plan:
            return ExecutionPlanStatus(
                summary=f"开仓条件已满足：币安挂 {_side_text(entry_plan.binance_side)} 限价 {entry_plan.limit_price}，数量 {entry_plan.quantity_oz} XAU；成交后 MT4 {_side_text(entry_plan.mt4_hedge_side)} 市价对冲，保护价 {entry_plan.mt4_price_limit}。",
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
            summary=f"等待开仓：币安高价差 {high_edge:.4f} 美元，币安低价差 {low_edge:.4f} 美元；任一方向达到 {settings.open_min_edge} 美元才挂单，挂单距离当前价 {settings.binance_entry_offset_usd} 美元。",
            max_follow_seconds=max_follow_seconds,
        )
    return ExecutionPlanStatus(summary="等待 Binance 和 MT4 报价齐全。", max_follow_seconds=max_follow_seconds)


def _quote_plan_block_reason(binance_quote: MarketQuote | None, mt4_quote: MarketQuote | None) -> str | None:
    checks = (("币安", binance_quote), ("MT4", mt4_quote))
    for label, quote in checks:
        check = risk.quote_fresh(quote)
        if not check.ok:
            return f"{label}报价未刷新（{_risk_reason_text(check.reason)}）"
    return None


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
