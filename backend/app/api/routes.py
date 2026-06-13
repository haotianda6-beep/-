import os
import threading
import time
from datetime import datetime, timezone
import logging

from fastapi import APIRouter, Header, HTTPException
from starlette.concurrency import run_in_threadpool

from app.core.credentials import CredentialStore
from app.core.env import ai_status, credential_statuses, env_bool
from app.core.models import AIInsight, BotSettings, DataSource, DeepSeekCredentialInput, ExchangeCredentialInput, ExchangeName, Mt4CredentialInput, RealtimeSnapshot, RiskEvent
from app.services.exchange_factory import build_ccxt_exchange, sanitize_exchange_error
from app.services.live_market_types import SPOT_EXCHANGE_IDS, SWAP_EXCHANGE_IDS
from app.services.mt4_bridge import Mt4QuoteIn, mt4_token_ok
from app.services.arbitrage_engine import ArbitrageEngine
from app.services.settings_store import SettingsStore

router = APIRouter(prefix="/api")
engine = ArbitrageEngine(SettingsStore())
credential_store = CredentialStore()
_SNAPSHOT_TTL_SECONDS = 1.0
_snapshot_lock = threading.Lock()
_snapshot_cache: RealtimeSnapshot | None = None
_snapshot_cache_at = 0.0
_snapshot_json_cache = ""
_snapshot_refreshing = False
logger = logging.getLogger(__name__)


@router.get("/snapshot", response_model=RealtimeSnapshot)
async def get_snapshot() -> RealtimeSnapshot:
    return await _snapshot()


@router.get("/cash-carry/opportunities")
async def get_cash_carry_opportunities():
    _, runtime = await _runtime()
    return runtime.cash_carry.opportunities if runtime else []


@router.get("/mt4-spread/opportunities")
async def get_mt4_spread_opportunities():
    if _lightweight_dashboard_enabled():
        return []
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
    return await run_in_threadpool(snapshot_cached)


def snapshot_cached() -> RealtimeSnapshot:
    global _snapshot_cache, _snapshot_cache_at, _snapshot_json_cache
    now = time.monotonic()
    if _snapshot_cache and now - _snapshot_cache_at <= _SNAPSHOT_TTL_SECONDS:
        return _snapshot_cache
    _start_snapshot_refresh()
    if _snapshot_cache:
        return _snapshot_cache
    snapshot = _loading_snapshot()
    _store_snapshot(snapshot)
    return snapshot


def snapshot_json_cached() -> str:
    snapshot_cached()
    return _snapshot_json_cache


def _start_snapshot_refresh() -> None:
    global _snapshot_refreshing
    if _snapshot_refreshing:
        return
    _snapshot_refreshing = True
    threading.Thread(target=_refresh_snapshot, daemon=True, name="snapshot-refresh").start()


def _refresh_snapshot() -> None:
    global _snapshot_refreshing
    if not _snapshot_lock.acquire(blocking=False):
        _snapshot_refreshing = False
        return
    try:
        if _lightweight_dashboard_enabled():
            _store_snapshot(_lightweight_snapshot())
        else:
            _store_snapshot(engine.snapshot())
    except Exception as exc:  # noqa: BLE001 - keep frontend responsive if any exchange call hangs/fails.
        logger.warning("snapshot refresh failed: %s", str(exc)[:220])
    finally:
        _snapshot_lock.release()
        _snapshot_refreshing = False


def _store_snapshot(snapshot: RealtimeSnapshot) -> None:
    global _snapshot_cache, _snapshot_cache_at, _snapshot_json_cache
    _snapshot_cache = snapshot
    _snapshot_cache_at = time.monotonic()
    _snapshot_json_cache = snapshot.model_dump_json()


def _loading_snapshot() -> RealtimeSnapshot:
    settings = engine.settings_store.load()
    now = datetime.now(timezone.utc)
    live_enabled = engine.live_read.live_data_enabled()
    return RealtimeSnapshot(
        balances=[],
        positions=[],
        cash_carry_opportunities=[],
        cash_carry_candidates=[],
        cash_carry_positions=[],
        mt4_spread_opportunities=[],
        mt4_spread_candidates=[],
        trades=engine.get_trades(),
        settings=settings,
        risk_events=[
            RiskEvent(
                id="snapshot-loading",
                severity="info",
                title="后台数据加载中",
                detail="交易所账户、机会扫描和持仓组合正在后台刷新，页面不会再等待全量扫描完成。",
                action="等待下一次自动刷新；若持续超过 1 分钟，检查交易所接口和后台日志。",
                created_at=now,
            )
        ],
        credential_status=credential_statuses(),
        ai_insight=AIInsight(
            provider="",
            model="",
            status="disabled",
            content="后台数据加载中，AI 风控等待真实快照刷新后更新。",
            updated_at=now,
            next_refresh_at=None,
        ),
        data_source=DataSource.LIVE if live_enabled else DataSource.MOCK,
    )


def _lightweight_dashboard_enabled() -> bool:
    return env_bool("MAIN_DASHBOARD_LIGHTWEIGHT", default=True)


def _lightweight_snapshot() -> RealtimeSnapshot:
    settings = engine.settings_store.load()
    now = datetime.now(timezone.utc)
    live_enabled = engine.live_read.live_data_enabled()
    runtime = engine.live_runtime.get(settings) if live_enabled and _lightweight_cash_carry_enabled() else None
    balances = runtime.account.balances if runtime else []
    positions = runtime.account.positions if runtime else []
    cash_opps = _trim(runtime.cash_carry.opportunities if runtime else [], 20)
    cash_candidates = _trim(runtime.cash_carry.candidates if runtime else [], 50)
    cash_prices = [*cash_opps, *cash_candidates]
    cash_positions = engine._cash_positions_snapshot(positions, cash_prices, settings) if live_enabled else []
    risk_events = [
        RiskEvent(
            id="main-lightweight-mode",
            severity="info",
            title="主控台轻量实时模式",
            detail="主控台只保留正向期现实时数据、参数和 API 管理；做单历史和 MT4 五所扫描不再参与首屏快照，避免后台缓存堆积导致无法进入。",
            action="黄金价差套利请进入 /xau-arb/ 独立执行器；需要恢复更多扫描时再单独开启。",
            created_at=now,
        )
    ]
    risk_events.extend(
        engine.get_risk_events(
            settings,
            runtime.account.issues if runtime else [],
            runtime.cash_carry.issues if runtime else ["正向期现实时扫描未启动"],
            [],
            cash_positions,
        )
    )
    return RealtimeSnapshot(
        balances=balances,
        positions=positions,
        cash_carry_opportunities=cash_opps,
        cash_carry_candidates=cash_candidates,
        cash_carry_positions=cash_positions,
        mt4_spread_opportunities=[],
        mt4_spread_candidates=[],
        trades=[],
        settings=settings,
        risk_events=risk_events,
        credential_status=credential_statuses(),
        ai_insight=AIInsight(
            provider="",
            model="",
            status="disabled",
            content="主控台已切换为轻量实时模式，AI 分析暂不参与首屏快照，避免外部调用影响页面进入。",
            updated_at=now,
            next_refresh_at=None,
        ),
        data_source=DataSource.LIVE if live_enabled else DataSource.MOCK,
    )


async def _runtime():
    settings = engine.settings_store.load()
    if _lightweight_dashboard_enabled() and not _lightweight_cash_carry_enabled():
        return settings, None
    if not engine.live_read.live_data_enabled():
        return settings, None
    return settings, engine.live_runtime.get(settings)


def _lightweight_cash_carry_enabled() -> bool:
    return env_bool("MAIN_DASHBOARD_CASH_CARRY_RUNTIME", default=True)


def _trim(items: list, limit: int) -> list:
    return list(items[:limit])
