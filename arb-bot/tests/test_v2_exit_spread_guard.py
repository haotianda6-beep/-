from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.binance_client import PaperBinanceClient
from app.config import Settings
from app.models import Mt4Tick, OpenPair, Side, StrategyState, utc_now_ms
from app.mt4_bridge import Mt4Bridge
from app.storage import Storage
from app.v2_executor import GoldV2Executor


def make_settings(tmp_path, **kwargs) -> Settings:
    values = {
        "PAPER_MODE": True,
        "LIVE_TRADING": False,
        "GOLD_V2_OBSERVATION_ONLY": False,
        "BINANCE_ENTRY_OFFSET_USD": Decimal("0.1"),
        "TARGET_OZ": Decimal("1"),
        "MT4_LOT_SIZE_OZ": Decimal("100"),
        "MT4_MIN_LOT": Decimal("0.01"),
        "MT4_LOT_STEP": Decimal("0.01"),
        "PAPER_AUTO_FILL": False,
        "MIN_ORDER_LIVE_MS": 0,
        "MAX_ORDER_AGE_MS": 0,
        "EXIT_CONFIRM_MS": 0,
        "MT4_SLIPPAGE_POINTS": 0,
        "MT4_CLOSE_EXTRA_BUFFER_USD": Decimal("0"),
        **kwargs,
    }
    return Settings(_env_file=None, **values)


def make_runtime() -> SimpleNamespace:
    return SimpleNamespace(
        state=StrategyState.PAIR_OPEN,
        last_error=None,
        open_pair=OpenPair(
            direction="BINANCE_SHORT_MT4_LONG",
            quantity_oz=Decimal("1"),
            binance_entry_price=Decimal("101"),
            mt4_entry_price=Decimal("99"),
            binance_order_id="entry_order",
            mt4_ticket=7,
            mt4_tickets=[7],
            base_edge=Decimal("2"),
        ),
    )


def make_executor(tmp_path, cfg: Settings | None = None) -> tuple[GoldV2Executor, PaperBinanceClient, Mt4Bridge, SimpleNamespace]:
    cfg = cfg or make_settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3")
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = make_runtime()
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    return executor, client, mt4, run


def set_quotes(client: PaperBinanceClient, mt4: Mt4Bridge, binance_bid: str, binance_ask: str, mt4_bid: str, mt4_ask: str) -> None:
    client.set_quote(Decimal(binance_bid), Decimal(binance_ask))
    mt4.update_tick(Mt4Tick(symbol="XAUUSD", bid=Decimal(mt4_bid), ask=Decimal(mt4_ask)))


@pytest.mark.asyncio
async def test_v2_regular_exit_never_quotes_when_spread_is_above_target(tmp_path):
    cfg = make_settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", EXIT_CONFIRM_MS=100)
    executor, client, mt4, run = make_executor(tmp_path, cfg)
    executor.exit_ready_since_ms = utc_now_ms() - 10_000
    set_quotes(client, mt4, "104", "104.2", "100", "100.2")

    await executor.step({"selected_entry": {"ready": False}, "exit_plan": {"enabled": True, "target_exit_spread": "2"}})

    assert run.state == StrategyState.PAIR_OPEN
    assert executor.active_order is None
    assert executor.exit_ready_since_ms == 0


@pytest.mark.asyncio
async def test_v2_open_exit_order_is_canceled_when_spread_worsens_before_fill(tmp_path):
    executor, client, mt4, run = make_executor(tmp_path)
    set_quotes(client, mt4, "100", "100.1", "99", "99.2")

    await executor.step({"selected_entry": {"ready": False}, "exit_plan": {"enabled": True, "target_exit_spread": "2"}})

    assert run.state == StrategyState.QUOTING_BINANCE_EXIT
    assert executor.active_order is not None
    assert executor.active_order.side == Side.BUY

    set_quotes(client, mt4, "104", "104.2", "100", "100.2")
    await executor.step({"selected_entry": {"ready": False}, "exit_plan": {"enabled": True, "target_exit_spread": "2"}})

    assert run.state == StrategyState.PAIR_OPEN
    assert executor.active_order is None
    events = Storage(executor.settings.sqlite_path).get_events(0, utc_now_ms(), limit=20)
    assert any(event["kind"] == "v2_order_canceled" and "平仓价差回落" in str(event["payload"]) for event in events)
