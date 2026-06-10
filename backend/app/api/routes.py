import os
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException
from starlette.concurrency import run_in_threadpool

from app.core.credentials import CredentialStore
from app.core.env import ai_status, credential_statuses
from app.core.models import BotSettings, DeepSeekCredentialInput, ExchangeCredentialInput, ExchangeName, Mt4CredentialInput, RealtimeSnapshot
from app.services.exchange_factory import build_ccxt_exchange, sanitize_exchange_error
from app.services.live_market_types import SPOT_EXCHANGE_IDS, SWAP_EXCHANGE_IDS
from app.services.mt4_bridge import Mt4QuoteIn, mt4_token_ok
from app.services.arbitrage_engine import ArbitrageEngine
from app.services.settings_store import SettingsStore

router = APIRouter(prefix="/api")
engine = ArbitrageEngine(SettingsStore())
credential_store = CredentialStore()


@router.get("/snapshot", response_model=RealtimeSnapshot)
async def get_snapshot() -> RealtimeSnapshot:
    return await _snapshot()


@router.get("/dashboard")
async def get_dashboard():
    settings = await run_in_threadpool(engine.settings_store.load)
    if await run_in_threadpool(engine.live_read.live_data_enabled):
        return []
    return await run_in_threadpool(engine.get_dashboard, settings)


@router.get("/opportunities")
async def get_opportunities():
    settings, runtime = await _runtime()
    return runtime.scan.opportunities if runtime else await run_in_threadpool(engine.get_opportunities, settings)


@router.get("/cash-carry/opportunities")
async def get_cash_carry_opportunities():
    _, runtime = await _runtime()
    return runtime.cash_carry.opportunities if runtime else []


@router.get("/reverse-cash-carry/opportunities")
async def get_reverse_cash_carry_opportunities():
    _, runtime = await _runtime()
    return runtime.reverse_cash_carry.opportunities if runtime else []


@router.get("/mt4-spread/opportunities")
async def get_mt4_spread_opportunities():
    _, runtime = await _runtime()
    return runtime.mt4_spread_opportunities if runtime else []


@router.post("/mt4/quote")
async def post_mt4_quote(payload: Mt4QuoteIn, x_mt4_token: str | None = Header(default=None)):
    if not mt4_token_ok(payload.token, x_mt4_token):
        raise HTTPException(status_code=403, detail="invalid mt4 token")
    quote = engine.mt4_quote_store.update(payload)
    return {"status": "ok", "symbol": quote.symbol, "timestamp": quote.timestamp}


@router.get("/trades")
async def get_trades():
    return await run_in_threadpool(engine.get_trades)


@router.get("/settings", response_model=BotSettings)
async def get_settings() -> BotSettings:
    return await run_in_threadpool(engine.settings_store.load)


@router.get("/exchanges/credentials")
async def get_exchange_credentials():
    return credential_statuses()


@router.get("/credentials")
async def get_credentials():
    return {
        "exchanges": credential_statuses(),
        "ai": {"deepseek": credential_store.deepseek_status()},
        "mt4": credential_store.mt4_status(),
        "server_public_ip": _server_public_ip(),
    }


@router.put("/credentials/exchanges/{exchange}")
async def save_exchange_credentials(exchange: str, payload: ExchangeCredentialInput):
    exchange_name = _parse_exchange(exchange)
    credential_store.save_exchange(exchange_name, payload)
    return {"status": "saved", "exchange": exchange_name, "credentials": credential_statuses()}


@router.delete("/credentials/exchanges/{exchange}")
async def delete_exchange_credentials(exchange: str):
    exchange_name = _parse_exchange(exchange)
    credential_store.delete_exchange(exchange_name)
    return {"status": "deleted", "exchange": exchange_name, "credentials": credential_statuses()}


@router.post("/credentials/exchanges/{exchange}/test")
async def test_exchange_credentials(exchange: str):
    exchange_name = _parse_exchange(exchange)
    status = next((item for item in credential_statuses() if ExchangeName(item.exchange) == exchange_name), None)
    if not status or not status.configured:
        raise HTTPException(status_code=400, detail="API 凭证未配置完整")
    ok, message = await run_in_threadpool(_test_exchange, exchange_name)
    credential_store.save_exchange_test(exchange_name, ok, message)
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"status": "ok", "exchange": exchange_name, "message": message, "tested_at": datetime.now(timezone.utc)}


@router.put("/credentials/ai/deepseek")
async def save_deepseek_credentials(payload: DeepSeekCredentialInput):
    result = credential_store.save_deepseek(payload.api_key, payload.base_url, payload.model)
    engine.ai_monitor.invalidate()
    return {"status": "saved", "deepseek": result}


@router.delete("/credentials/ai/deepseek")
async def delete_deepseek_credentials():
    credential_store.delete_deepseek()
    engine.ai_monitor.invalidate()
    return {"status": "deleted", "deepseek": credential_store.deepseek_status()}


@router.put("/credentials/mt4")
async def save_mt4_credentials(payload: Mt4CredentialInput):
    return {"status": "saved", "mt4": credential_store.save_mt4_token(payload.bridge_token)}


@router.delete("/credentials/mt4")
async def delete_mt4_credentials():
    credential_store.delete_mt4_token()
    return {"status": "deleted", "mt4": credential_store.mt4_status()}


@router.get("/ai/status")
async def get_ai_status():
    return ai_status()


@router.post("/ai/refresh")
async def refresh_ai():
    engine.ai_monitor.invalidate()
    return {"status": "refreshing"}


@router.put("/settings", response_model=BotSettings)
async def update_settings(settings: BotSettings) -> BotSettings:
    return engine.update_settings(settings)


def _parse_exchange(exchange: str) -> ExchangeName:
    try:
        return ExchangeName(exchange.upper())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="unsupported exchange") from exc


def _test_exchange(exchange: ExchangeName) -> tuple[bool, str]:
    checks = (("现货", SPOT_EXCHANGE_IDS[exchange], "spot"), ("合约", SWAP_EXCHANGE_IDS[exchange], "swap"))
    messages: list[str] = []
    for label, exchange_id, default_type in checks:
        try:
            exchange_client = build_ccxt_exchange(exchange, exchange_id, default_type, timeout=15000)
            exchange_client.fetch_balance()
            messages.append(f"{label}余额读取正常")
        except Exception as exc:  # noqa: BLE001 - ccxt raises exchange-specific errors.
            clean = sanitize_exchange_error(str(exc))[:180]
            return False, f"{label}接口测试失败：{clean}"
    return True, "，".join(messages)


def _server_public_ip() -> str:
    return os.getenv("SERVER_PUBLIC_IP", "").strip()


async def _snapshot() -> RealtimeSnapshot:
    return await run_in_threadpool(engine.snapshot)


async def _runtime():
    settings = await run_in_threadpool(engine.settings_store.load)
    if not await run_in_threadpool(engine.live_read.live_data_enabled):
        return settings, None
    return settings, await run_in_threadpool(engine.live_runtime.get, settings)
