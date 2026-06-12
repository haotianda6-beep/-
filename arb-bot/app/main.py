from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse

from app.binance_client import BinanceFuturesClient, PaperBinanceClient
from app.config import Settings, existing_env_paths, load_settings, update_local_config_file
from app.logger import setup_logging
from app.models import EngineStatus, MarketQuote, Mt4Report, Mt4Tick, RuntimeConfig, RuntimeConfigUpdate
from app.mt4_bridge import Mt4Bridge
from app.risk import RiskManager
from app.storage import Storage
from app.strategy import StrategyEngine


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
    await binance_client.stop()


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
        binance_quote=binance_client.latest_quote(),
        mt4_quote=mt4_bridge.latest_quote(),
        open_pair=strategy.open_pair,
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
