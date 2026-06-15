from decimal import Decimal

import pytest

from app.binance_client import PaperBinanceClient
from app.config import Settings
from app.models import MarketQuote, Mt4Tick, OrderStatus, StrategyState, utc_now_ms
from app.mt4_bridge import Mt4Bridge
from app.risk import RiskManager
from app.storage import Storage
from app.strategy import StrategyEngine


def settings(tmp_path, **kwargs) -> Settings:
    return Settings(
        _env_file=None,
        PAPER_MODE=True,
        LIVE_TRADING=False,
        PAPER_AUTO_FILL=False,
        SQLITE_PATH=tmp_path / "test.sqlite3",
        MAX_QUOTE_AGE_MS=500,
        **kwargs,
    )


def test_live_guard_requires_explicit_live_and_credentials(tmp_path):
    cfg = settings(tmp_path)
    risk = RiskManager(cfg, Storage(cfg.sqlite_path))
    result = risk.live_ready(binance_ready=True, mt4_connected=True, maker_fee_loaded=True)
    assert not result.ok
    assert "dry-run" in result.reason


@pytest.mark.asyncio
async def test_stale_quote_cancels_unfilled_entry_and_waits_again(tmp_path):
    cfg = settings(tmp_path)
    client = PaperBinanceClient(cfg)
    client.set_quote(Decimal("2001"), Decimal("2002"))
    mt4 = Mt4Bridge(cfg)
    mt4.update_tick(Mt4Tick(symbol="XAUUSD", bid=Decimal("1999"), ask=Decimal("2000")))
    store = Storage(cfg.sqlite_path)
    engine = StrategyEngine(cfg, client, mt4, RiskManager(cfg, store), store)
    await engine.step()
    assert engine.active_order is not None
    order_id = engine.active_order.order_id
    client._quote = MarketQuote(
        symbol=cfg.binance_symbol,
        bid=Decimal("2001"),
        ask=Decimal("2002"),
        timestamp_ms=utc_now_ms() - 1000,
    )
    await engine.step()
    assert engine.state == StrategyState.IDLE
    assert engine.active_order is None
    canceled = await client.get_order(order_id)
    assert canceled is not None
    assert canceled.status == OrderStatus.CANCELED
