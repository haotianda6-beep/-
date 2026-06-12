from decimal import Decimal

import pytest

from app.binance_client import PaperBinanceClient
from app.config import Settings
from app.models import ExchangeFilters, MarketQuote, Mt4Report, Mt4Tick, OrderStatus, PairDirection, Side, StrategyState, utc_now_ms
from app.mt4_bridge import Mt4Bridge
from app.risk import RiskManager
from app.storage import Storage
from app.strategy import StrategyEngine, build_entry_plan


def settings(tmp_path, **kwargs) -> Settings:
    values = {
        "PAPER_MODE": True,
        "LIVE_TRADING": False,
        "SQLITE_PATH": tmp_path / "test.sqlite3",
        "PAPER_AUTO_FILL": False,
        "TARGET_OZ": Decimal("1"),
        "MT4_LOT_SIZE_OZ": Decimal("100"),
        **kwargs,
    }
    return Settings(_env_file=None, **values)


def filters() -> ExchangeFilters:
    return ExchangeFilters(tick_size=Decimal("0.1"), qty_step=Decimal("0.001"), min_qty=Decimal("0.001"))


def test_binance_high_entry_formula(tmp_path):
    cfg = settings(tmp_path, OPEN_MIN_EDGE=Decimal("1.50"), MIN_LOCKED_EDGE=Decimal("0.80"))
    plan = build_entry_plan(
        cfg,
        filters(),
        MarketQuote(symbol="XAUUSDT", bid=Decimal("2001"), ask=Decimal("2002")),
        MarketQuote(symbol="XAUUSD", bid=Decimal("1999"), ask=Decimal("2000")),
    )
    assert plan is not None
    assert plan.direction == PairDirection.BINANCE_SHORT_MT4_LONG
    assert plan.binance_side == Side.SELL
    assert plan.limit_price == Decimal("2002.0")
    assert plan.mt4_hedge_side == Side.BUY
    assert plan.mt4_price_limit == Decimal("2001.2")


def test_binance_low_entry_formula(tmp_path):
    cfg = settings(tmp_path, OPEN_MIN_EDGE=Decimal("1.50"), MIN_LOCKED_EDGE=Decimal("0.80"))
    plan = build_entry_plan(
        cfg,
        filters(),
        MarketQuote(symbol="XAUUSDT", bid=Decimal("1998"), ask=Decimal("1999")),
        MarketQuote(symbol="XAUUSD", bid=Decimal("2000"), ask=Decimal("2001")),
    )
    assert plan is not None
    assert plan.direction == PairDirection.BINANCE_LONG_MT4_SHORT
    assert plan.binance_side == Side.BUY
    assert plan.limit_price == Decimal("1998.0")
    assert plan.mt4_hedge_side == Side.SELL
    assert plan.mt4_price_limit == Decimal("1998.8")


@pytest.mark.asyncio
async def test_partial_fill_hedges_only_filled_quantity(tmp_path):
    engine, client, mt4 = await make_engine(tmp_path)
    await engine.step()
    order = engine.active_order
    assert order is not None
    await client.simulate_fill(order.order_id, Decimal("0.4"), Decimal("2002"))
    await engine.step()
    command = mt4.next_command()
    assert command["action"] == "BUY"
    assert Decimal(str(command["lots"])) == Decimal("0.004")
    assert engine.state == StrategyState.HEDGING_MT4


@pytest.mark.asyncio
async def test_mt4_failure_triggers_emergency_close(tmp_path):
    engine, client, mt4 = await make_engine(tmp_path)
    await engine.step()
    order = engine.active_order
    assert order is not None
    await client.simulate_fill(order.order_id, Decimal("0.4"), Decimal("2002"))
    await engine.step()
    command = mt4.next_command()
    mt4.submit_report(Mt4Report(command_id=command["command_id"], status="error", action="BUY", message="off quotes"))
    await engine.step()
    assert engine.state == StrategyState.PAUSED
    emergency = [item for item in client._orders.values() if item.is_maker is False and item.reduce_only]
    assert emergency
    assert emergency[-1].side == Side.BUY
    assert emergency[-1].executed_qty == Decimal("0.4")


@pytest.mark.asyncio
async def test_dry_run_does_not_send_real_mt4_order(tmp_path):
    cfg = settings(tmp_path, PAPER_MODE=True, LIVE_TRADING=False)
    client = PaperBinanceClient(cfg)
    client.set_quote(Decimal("2001"), Decimal("2002"))
    mt4 = Mt4Bridge(cfg)
    mt4.update_tick(Mt4Tick(symbol="XAUUSD", bid=Decimal("1999"), ask=Decimal("2000")))
    store = Storage(cfg.sqlite_path)
    engine = StrategyEngine(cfg, client, mt4, RiskManager(cfg, store), store)

    await engine.step()
    order = engine.active_order
    assert order is not None
    await client.simulate_fill(order.order_id, Decimal("0.4"), Decimal("2002"))
    await engine.step()

    assert mt4.next_command() == {"command": "NONE"}
    assert engine.state == StrategyState.PAIR_OPEN
    assert engine.open_pair is not None
    assert engine.open_pair.quantity_oz == Decimal("0.4")


async def make_engine(tmp_path):
    cfg = settings(tmp_path, PAPER_MODE=False, LIVE_TRADING=True)
    client = PaperBinanceClient(cfg)
    client.set_quote(Decimal("2001"), Decimal("2002"))
    mt4 = Mt4Bridge(cfg)
    mt4.update_tick(Mt4Tick(symbol="XAUUSD", bid=Decimal("1999"), ask=Decimal("2000")))
    store = Storage(cfg.sqlite_path)
    engine = StrategyEngine(cfg, client, mt4, RiskManager(cfg, store), store)
    await client.start()
    return engine, client, mt4
