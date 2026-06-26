from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.binance_client import BinanceError, PaperBinanceClient
from app.config import Settings
from app.models import Mt4Position, Mt4Report, Mt4Tick, OpenPair, OrderRequest, OrderStatus, OrderUpdate, Side, StrategyState, utc_now_ms
from app.mt4_bridge import Mt4Bridge
from app.storage import Storage
from app.v2_executor import GoldV2Executor, REQUIRED_MT4_EA_VERSION


def settings(tmp_path, **kwargs) -> Settings:
    values = {
        "PAPER_MODE": True,
        "LIVE_TRADING": False,
        "GOLD_V2_OBSERVATION_ONLY": False,
        "BINANCE_ENTRY_OFFSET_USD": Decimal("0.1"),
        "TARGET_OZ": Decimal("1"),
        "MT4_LOT_SIZE_OZ": Decimal("100"),
        "MT4_MIN_LOT": Decimal("0.01"),
        "MT4_LOT_STEP": Decimal("0.01"),
        "PAPER_AUTO_FILL": True,
        "PAPER_FILL_DELAY_MS": 0,
        "MIN_ORDER_LIVE_MS": 0,
        "MAX_ORDER_AGE_MS": 0,
        "MAX_HEDGE_DELAY_MS": 1000,
        "ENTRY_CONFIRM_MS": 0,
        "RISK_EXIT_CONFIRM_MS": 0,
        "CLOSE_PROFIT_USD_PER_OZ": Decimal("0.5"),
        "MT4_SLIPPAGE_POINTS": 0,
        "MT4_CLOSE_EXTRA_BUFFER_USD": Decimal("0"),
        **kwargs,
    }
    return Settings(_env_file=None, **values)


def runtime():
    return SimpleNamespace(state=StrategyState.IDLE, last_error=None, open_pair=None)


class AuditPaperBinanceClient(PaperBinanceClient):
    def __init__(self, settings: Settings, rows: list[dict]) -> None:
        super().__init__(settings)
        self.rows = rows

    async def user_trades(self, start_ms: int, end_ms: int, limit: int = 1000) -> list[dict]:
        return self.rows


def mt4_tick(
    mt4: Mt4Bridge,
    bid: str,
    ask: str,
    trade_allowed: bool | None = None,
    symbol_trade_allowed: bool | None = None,
    terminal_trade_allowed: bool | None = None,
    trade_context_busy: bool | None = None,
    ea_version: str | None = None,
) -> None:
    mt4.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            ea_version=ea_version,
            bid=Decimal(bid),
            ask=Decimal(ask),
            trade_allowed=trade_allowed,
            symbol_trade_allowed=symbol_trade_allowed,
            terminal_trade_allowed=terminal_trade_allowed,
            trade_context_busy=trade_context_busy,
        )
    )


@pytest.mark.asyncio
async def test_v2_binance_fill_audit_records_maker_without_fee(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", PAPER_MODE=False, LIVE_TRADING=True)
    store = Storage(cfg.sqlite_path)
    client = AuditPaperBinanceClient(
        cfg,
        [{"orderId": "entry-1", "id": "trade-1", "maker": True, "commission": "0", "commissionAsset": "USDT"}],
    )
    executor = GoldV2Executor(cfg, client, Mt4Bridge(cfg), store, runtime())
    order = OrderUpdate(
        order_id="entry-1",
        client_order_id="arb_entry",
        symbol="XAUUSDT",
        side=Side.SELL,
        status=OrderStatus.FILLED,
        price=Decimal("101"),
        orig_qty=Decimal("1"),
        executed_qty=Decimal("1"),
        avg_price=Decimal("101"),
    )

    await executor._audit_binance_fill(order, "entry")

    events = store.get_events(0, utc_now_ms() + 1_000, limit=20)
    audit = [event for event in events if event["kind"] == "v2_binance_fill_audit"]
    risk = [event for event in events if event["kind"] == "v2_binance_fee_or_taker_detected"]
    assert audit[-1]["payload"]["all_maker"] is True
    assert audit[-1]["payload"]["commission"] == "0"
    assert not risk


@pytest.mark.asyncio
async def test_v2_binance_fill_audit_flags_fee_or_taker(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", PAPER_MODE=False, LIVE_TRADING=True)
    store = Storage(cfg.sqlite_path)
    client = AuditPaperBinanceClient(
        cfg,
        [{"orderId": "exit-1", "id": "trade-2", "maker": False, "commission": "1.23", "commissionAsset": "USDT"}],
    )
    executor = GoldV2Executor(cfg, client, Mt4Bridge(cfg), store, runtime())
    order = OrderUpdate(
        order_id="exit-1",
        client_order_id="arb_exit",
        symbol="XAUUSDT",
        side=Side.BUY,
        status=OrderStatus.FILLED,
        price=Decimal("100"),
        orig_qty=Decimal("1"),
        executed_qty=Decimal("1"),
        avg_price=Decimal("100"),
        reduce_only=True,
    )

    await executor._audit_binance_fill(order, "exit")

    events = store.get_events(0, utc_now_ms() + 1_000, limit=20)
    risk = [event for event in events if event["kind"] == "v2_binance_fee_or_taker_detected"]
    assert risk[-1]["payload"]["all_maker"] is False
    assert risk[-1]["payload"]["commission"] == "1.23"
    assert risk[-1]["payload"]["non_maker_trade_ids"] == ["trade-2"]


def short_plan(price: str = "101") -> dict:
    return {
        "selected_entry": {
            "ready": True,
            "direction": "BINANCE_SHORT_MT4_LONG",
            "binance_side": "SELL",
            "binance_price": price,
            "quantity_oz": "1",
            "mt4_follow_side": "BUY",
        }
    }


def guarded_short_plan(price: str = "101", locked_floor: str = "2") -> dict:
    plan = short_plan(price)
    plan["selected_entry"].update(
        {
            "locked_edge_floor": locked_floor,
            "mt4_slippage_budget": "0",
        }
    )
    return plan


def add_plan(price: str = "105", actionable: str | None = None) -> dict:
    plan = {
        "selected_entry": {"ready": False, "reason": "已有持仓"},
        "add_plan": {
            "enabled": True,
            "ready": True,
            "base_edge": "2",
            "next_trigger_edge": "3",
            "quantity_oz": "1",
            "binance_side": "SELL",
            "binance_price": price,
            "mt4_follow_side": "BUY",
        },
    }
    if actionable is not None:
        plan["add_plan"]["next_actionable_trigger_edge"] = actionable
    return plan


def add_plan_temporarily_blocked_after_trigger(reason: str = "补仓后仍不安全") -> dict:
    return {
        "selected_entry": {"ready": False, "reason": "已有持仓"},
        "add_plan": {
            "enabled": True,
            "ready": False,
            "reason": reason,
            "base_edge": "2",
            "current_edge": "3.5",
            "next_trigger_edge": "3",
            "quantity_oz": "1",
            "binance_side": "SELL",
            "binance_price": "105",
            "mt4_follow_side": "BUY",
        },
    }


def add_plan_reached_base_trigger_before_actionable_price() -> dict:
    return {
        "selected_entry": {"ready": False, "reason": "已有持仓"},
        "add_plan": {
            "enabled": True,
            "ready": True,
            "reason": "达到补仓触发位，可以挂补仓限价单。",
            "base_edge": "2",
            "current_edge": "3.2",
            "next_trigger_edge": "3",
            "next_actionable_trigger_edge": "3.8",
            "quantity_oz": "1",
            "binance_side": "SELL",
            "binance_price": "105",
            "mt4_follow_side": "BUY",
        },
    }


def add_plan_actionable_after_base_trigger(price: str = "105") -> dict:
    return {
        "selected_entry": {"ready": False, "reason": "已有持仓"},
        "add_plan": {
            "enabled": True,
            "ready": True,
            "base_edge": "2",
            "current_edge": "4.0",
            "next_trigger_edge": "3",
            "next_actionable_trigger_edge": "3.8",
            "quantity_oz": "1",
            "binance_side": "SELL",
            "binance_price": price,
            "mt4_follow_side": "BUY",
        },
    }


def exit_plan(target: str = "2", estimated_net: str = "2") -> dict:
    return {
        "selected_entry": {"ready": False},
        "exit_plan": {"enabled": True, "target_exit_spread": target, "estimated_net": estimated_net},
    }


def test_v2_weak_pair_uses_relaxed_minimum_exit_net(tmp_path):
    cfg = settings(
        tmp_path,
        SQLITE_PATH=tmp_path / "test.sqlite3",
        OPEN_MIN_EDGE=Decimal("2.40"),
        CLOSE_PROFIT_USD_PER_OZ=Decimal("1.21"),
        AGED_CLOSE_PROFIT_USD_PER_OZ=Decimal("0.10"),
        MAX_PAIR_AGE_MINUTES=60,
    )
    pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("2"),
        binance_entry_price=Decimal("4059.61"),
        mt4_entry_price=Decimal("4057.96"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("1.65"),
    )
    executor = GoldV2Executor(cfg, PaperBinanceClient(cfg), Mt4Bridge(cfg), Storage(cfg.sqlite_path), runtime())

    assert executor._effective_close_profit_usd_per_oz(pair) == Decimal("0.10")
    assert executor._minimum_exit_net(pair) == Decimal("0.20")


def test_v2_good_pair_keeps_full_minimum_exit_net(tmp_path):
    cfg = settings(
        tmp_path,
        SQLITE_PATH=tmp_path / "test.sqlite3",
        OPEN_MIN_EDGE=Decimal("2.40"),
        CLOSE_PROFIT_USD_PER_OZ=Decimal("1.21"),
        AGED_CLOSE_PROFIT_USD_PER_OZ=Decimal("0.10"),
        MAX_PAIR_AGE_MINUTES=60,
    )
    pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("2"),
        binance_entry_price=Decimal("4059.61"),
        mt4_entry_price=Decimal("4056.61"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("3.00"),
    )
    executor = GoldV2Executor(cfg, PaperBinanceClient(cfg), Mt4Bridge(cfg), Storage(cfg.sqlite_path), runtime())

    assert executor._effective_close_profit_usd_per_oz(pair) == Decimal("1.21")
    assert executor._minimum_exit_net(pair) == Decimal("2.42")


class MissingOrderBinanceClient(PaperBinanceClient):
    async def get_order(self, order_id: str) -> OrderUpdate | None:
        raise BinanceError('{"code":-2013,"msg":"Order does not exist."}')


@pytest.mark.asyncio
async def test_v2_entry_and_exit_use_binance_post_only_then_mt4_follow(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3")
    store = Storage(cfg.sqlite_path)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    executor = GoldV2Executor(cfg, client, mt4, store, run)

    client.set_quote(Decimal("100"), Decimal("100.2"))
    mt4_tick(mt4, "98.8", "99")
    await executor.step(short_plan("101"))
    assert run.state == StrategyState.QUOTING_BINANCE_ENTRY

    client.set_quote(Decimal("101"), Decimal("101.2"))
    await executor.step(short_plan("101"))
    command = mt4.next_command()
    assert command["action"] == "BUY"
    assert command["lots"] == "0.01"
    queued_events = [event for event in store.get_events(0, utc_now_ms() + 1_000, limit=100) if event["kind"] == "v2_mt4_entry_queued"]
    assert queued_events[-1]["payload"]["mt4_quote_at_command"]["ask"] == "99"

    mt4.submit_report(Mt4Report(command_id=command["command_id"], status="ok", action="BUY", ticket=7, fill_price=Decimal("99"), lots=Decimal("0.01")))
    await executor.step(short_plan("101"))
    assert run.state == StrategyState.PAIR_OPEN
    assert run.open_pair is not None
    assert run.open_pair.base_edge == Decimal("2")
    executor.post_add_exit_block_until_ms = 0

    client.set_quote(Decimal("100"), Decimal("100.1"))
    mt4_tick(mt4, "99", "99.3")
    await executor.step(exit_plan("2"))
    assert run.state == StrategyState.QUOTING_BINANCE_EXIT
    assert executor.active_order is not None
    assert executor.active_order.reduce_only is True
    assert executor.active_order.side == Side.BUY

    client.set_quote(Decimal("99.8"), executor.active_order.price)
    await executor.step(exit_plan("2"))
    close_command = mt4.next_command()
    assert close_command["action"] == "CLOSE"
    assert close_command["ticket"] == 7

    mt4.submit_report(Mt4Report(command_id=close_command["command_id"], status="ok", action="CLOSE", ticket=7, fill_price=Decimal("99.2"), lots=Decimal("0.01")))
    await executor.step(exit_plan("2"))
    assert run.state == StrategyState.IDLE
    assert run.open_pair is None
    assert all(order.is_maker for order in client._orders.values())
    assert store.daily_pnl() == Decimal("1.3")
    events = store.get_events(0, utc_now_ms() + 1_000, limit=100)
    entry_slippage_events = [event for event in events if event["kind"] == "v2_mt4_entry_slippage"]
    assert entry_slippage_events
    assert entry_slippage_events[-1]["payload"]["reference_price"] == "99"
    assert entry_slippage_events[-1]["payload"]["mt4_entry_adverse_slippage"] == "0"
    pnl_events = [event for event in events if event["kind"] == "v2_pair_pnl_recorded"]
    assert pnl_events
    pnl_payload = pnl_events[-1]["payload"]
    assert pnl_payload["entry_spread"] == "2"
    assert pnl_payload["actual_exit_spread"] == "0.7"
    assert pnl_payload["mt4_close_quote"] == "99"
    assert pnl_payload["mt4_close_adverse_slippage"] == "-0.2"
    assert pnl_payload["binance_to_mt4_latency_ms"] is not None
    assert pnl_payload["mt4_command_to_report_latency_ms"] is not None


@pytest.mark.asyncio
async def test_v2_blocks_new_entry_during_min_entry_interval(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", GOLD_V2_MIN_ENTRY_INTERVAL_MS=10_000)
    store = Storage(cfg.sqlite_path)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    executor = GoldV2Executor(cfg, client, mt4, store, run)
    executor.last_entry_ms = utc_now_ms()

    client.set_quote(Decimal("100"), Decimal("100.2"))
    mt4_tick(mt4, "98.8", "99")
    await executor.step(short_plan("101"))

    assert run.state == StrategyState.IDLE
    assert executor.active_order is None
    assert "开仓频率控制中" in run.last_error


@pytest.mark.asyncio
async def test_v2_updates_last_entry_time_after_mt4_follow(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", GOLD_V2_MIN_ENTRY_INTERVAL_MS=10_000)
    store = Storage(cfg.sqlite_path)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    executor = GoldV2Executor(cfg, client, mt4, store, run)

    client.set_quote(Decimal("100"), Decimal("100.2"))
    mt4_tick(mt4, "98.8", "99")
    await executor.step(short_plan("101"))
    client.set_quote(Decimal("101"), Decimal("101.2"))
    await executor.step(short_plan("101"))
    command = mt4.next_command()
    mt4.submit_report(Mt4Report(command_id=command["command_id"], status="ok", action="BUY", ticket=7, fill_price=Decimal("99"), lots=Decimal("0.01")))
    await executor.step(short_plan("101"))

    assert run.open_pair is not None
    assert executor.last_entry_ms == run.open_pair.opened_ms


@pytest.mark.asyncio
async def test_v2_entry_sets_short_exit_cooldown_after_mt4_follow(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3")
    store = Storage(cfg.sqlite_path)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    executor = GoldV2Executor(cfg, client, mt4, store, run)

    client.set_quote(Decimal("100"), Decimal("100.2"))
    mt4_tick(mt4, "98.8", "99")
    await executor.step(short_plan("101"))
    client.set_quote(Decimal("101"), Decimal("101.2"))
    await executor.step(short_plan("101"))
    command = mt4.next_command()
    mt4.submit_report(Mt4Report(command_id=command["command_id"], status="ok", action="BUY", ticket=7, fill_price=Decimal("99"), lots=Decimal("0.01")))
    await executor.step(short_plan("101"))

    client.set_quote(Decimal("100"), Decimal("100.1"))
    mt4_tick(mt4, "99", "99.3")
    await executor.step(exit_plan("2"))

    assert run.state == StrategyState.PAIR_OPEN
    assert executor.active_order is None
    assert "等待仓位和 MT4 报价稳定" in run.last_error


@pytest.mark.asyncio
async def test_v2_entry_cancel_sets_requote_cooldown(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", PAPER_AUTO_FILL=False, REQUOTE_COOLDOWN_MS=5000)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    client.set_quote(Decimal("100"), Decimal("100.2"))
    mt4_tick(mt4, "98.8", "99")

    await executor.step(short_plan("101"))
    assert run.state == StrategyState.QUOTING_BINANCE_ENTRY

    await executor.step({"selected_entry": {"ready": False, "reason": "价差回落"}})
    assert run.state == StrategyState.IDLE
    assert executor.active_order is None

    await executor.step(short_plan("101"))
    assert executor.active_order is None
    assert "开仓撤单冷却中" in run.last_error

    executor.entry_requote_until_ms = utc_now_ms() - 1
    await executor.step(short_plan("101"))
    assert run.state == StrategyState.QUOTING_BINANCE_ENTRY
    assert executor.active_order is not None


@pytest.mark.asyncio
async def test_v2_cancels_unfilled_entry_when_resting_order_locked_edge_turns_bad(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", PAPER_AUTO_FILL=False, REQUOTE_COOLDOWN_MS=0)
    store = Storage(cfg.sqlite_path)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    executor = GoldV2Executor(cfg, client, mt4, store, run)
    client.set_quote(Decimal("100"), Decimal("100.2"))
    mt4_tick(mt4, "98.8", "99")

    await executor.step(guarded_short_plan("101", locked_floor="2"))
    assert run.state == StrategyState.QUOTING_BINANCE_ENTRY
    assert executor.active_order is not None

    mt4_tick(mt4, "100.4", "100.6")
    await executor.step(guarded_short_plan("101", locked_floor="2"))

    assert run.state == StrategyState.IDLE
    assert executor.active_order is None
    events = store.get_events(0, utc_now_ms() + 1_000, limit=50)
    canceled = [event for event in events if event["kind"] == "v2_order_canceled"]
    assert canceled
    assert "锁定价差" in canceled[-1]["payload"]["reason"]


@pytest.mark.asyncio
async def test_v2_keeps_unfilled_entry_when_resting_order_locked_edge_still_safe(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", PAPER_AUTO_FILL=False)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    client.set_quote(Decimal("100"), Decimal("100.2"))
    mt4_tick(mt4, "98.8", "99")

    await executor.step(guarded_short_plan("101", locked_floor="2"))
    mt4_tick(mt4, "98.7", "98.9")
    await executor.step(guarded_short_plan("101", locked_floor="2"))

    assert run.state == StrategyState.QUOTING_BINANCE_ENTRY
    assert executor.active_order is not None


@pytest.mark.asyncio
async def test_v2_does_not_place_exit_until_real_exit_plan_is_ready(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3")
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    client.set_quote(Decimal("100"), Decimal("100.1"))
    mt4_tick(mt4, "99", "99.2")

    await executor.step({"selected_entry": {"ready": False}})

    assert run.state == StrategyState.PAIR_OPEN
    assert executor.active_order is None
    assert "真实均价" in run.last_error


@pytest.mark.asyncio
async def test_v2_regular_exit_requires_estimated_net_before_order(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", PAPER_AUTO_FILL=False, EXIT_CONFIRM_MS=0)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    client.set_quote(Decimal("100"), Decimal("100.1"))
    mt4_tick(mt4, "99", "99.2")

    await executor.step({"selected_entry": {"ready": False}, "exit_plan": {"enabled": True, "target_exit_spread": "2"}})

    assert run.state == StrategyState.PAIR_OPEN
    assert executor.active_order is None
    assert "等待预估净值" in run.last_error


@pytest.mark.asyncio
async def test_v2_regular_exit_blocks_limit_price_that_would_not_meet_min_profit(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", PAPER_AUTO_FILL=False, EXIT_CONFIRM_MS=0)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    client.set_quote(Decimal("100"), Decimal("100.1"))
    mt4_tick(mt4, "97.2", "97.4")

    await executor.step(exit_plan("3.2"))

    assert run.state == StrategyState.PAIR_OPEN
    assert executor.active_order is None
    assert "平仓利润保护" in run.last_error


@pytest.mark.asyncio
async def test_v2_regular_exit_does_not_subtract_mt4_close_buffer_twice(tmp_path):
    cfg = settings(
        tmp_path,
        SQLITE_PATH=tmp_path / "test.sqlite3",
        PAPER_AUTO_FILL=False,
        EXIT_CONFIRM_MS=0,
        MT4_CLOSE_EXTRA_BUFFER_USD=Decimal("2"),
    )
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    client.set_quote(Decimal("100"), Decimal("100.1"))
    mt4_tick(mt4, "99", "99.2")

    await executor.step(exit_plan("3.2"))

    assert run.state == StrategyState.QUOTING_BINANCE_EXIT
    assert executor.active_order is not None
    assert run.last_error is None


@pytest.mark.asyncio
async def test_v2_mt4_market_closed_blocks_repeated_exit_and_records_binance_pnl(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", PAPER_AUTO_FILL=False, EXIT_CONFIRM_MS=0)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.CLOSING_MT4
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    executor.active_order = OrderUpdate(
        order_id="exit_order",
        client_order_id="exit_client",
        symbol="XAUUSDT",
        side=Side.BUY,
        status=OrderStatus.FILLED,
        price=Decimal("102"),
        orig_qty=Decimal("1"),
        executed_qty=Decimal("1"),
        avg_price=Decimal("102"),
        reduce_only=True,
    )
    executor.close_command_tickets = {"close_cmd": 7}
    executor.pending_close_tickets = {7}
    mt4.submit_report(
        Mt4Report(
            command_id="close_cmd",
            status="error",
            action="CLOSE",
            ticket=7,
            error_code=132,
            message="OrderClose failed",
        )
    )

    await executor.step({})

    assert run.state == StrategyState.PAIR_OPEN
    assert executor.active_order is None
    assert executor.pending_close_tickets == set()
    assert mt4.next_command() == {"command": "NONE"}
    assert run.open_pair.realized_pnl == Decimal("-1")
    assert "MT4 暂不可平仓" in run.last_error

    client.set_quote(Decimal("100"), Decimal("100.1"))
    mt4_tick(mt4, "99", "99.2")
    await executor.step(exit_plan("2"))

    assert run.state == StrategyState.PAIR_OPEN
    assert executor.active_order is None
    assert "禁止币安平仓" in run.last_error


@pytest.mark.asyncio
async def test_v2_mt4_trade_block_prevents_add_order(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", PAPER_AUTO_FILL=False, MAX_ADD_COUNT=2)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    executor.mt4_exit_block_until_ms = utc_now_ms() + 60_000
    client.set_quote(Decimal("104"), Decimal("104.2"))
    mt4_tick(mt4, "100", "100.2", ea_version=REQUIRED_MT4_EA_VERSION)

    await executor.step(add_plan("105", actionable="3.8"))

    assert run.state == StrategyState.PAIR_OPEN
    assert executor.active_order is None
    assert "禁止币安补仓" in run.last_error


@pytest.mark.asyncio
async def test_v2_live_requires_mt4_trade_allowed_before_add_order(tmp_path):
    cfg = settings(
        tmp_path,
        SQLITE_PATH=tmp_path / "test.sqlite3",
        PAPER_MODE=False,
        LIVE_TRADING=True,
        PAPER_AUTO_FILL=False,
        MAX_ADD_COUNT=2,
    )
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    client.set_quote(Decimal("104"), Decimal("104.2"))
    mt4_tick(mt4, "100", "100.2", ea_version=REQUIRED_MT4_EA_VERSION)

    await executor.step(add_plan("105", actionable="3.8"))

    assert run.state == StrategyState.PAIR_OPEN
    assert executor.active_order is None
    assert "交易状态未确认可交易" in run.last_error


@pytest.mark.asyncio
async def test_v2_live_requires_current_mt4_ea_version_before_add_order(tmp_path):
    cfg = settings(
        tmp_path,
        SQLITE_PATH=tmp_path / "test.sqlite3",
        PAPER_MODE=False,
        LIVE_TRADING=True,
        PAPER_AUTO_FILL=False,
        MAX_ADD_COUNT=2,
    )
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    client.set_quote(Decimal("104"), Decimal("104.2"))
    mt4_tick(
        mt4,
        "100",
        "100.2",
        trade_allowed=True,
        symbol_trade_allowed=True,
        terminal_trade_allowed=True,
        trade_context_busy=False,
        ea_version="20260626-trade-guard",
    )

    await executor.step(add_plan("105", actionable="3.8"))

    assert run.state == StrategyState.PAIR_OPEN
    assert executor.active_order is None
    assert REQUIRED_MT4_EA_VERSION in run.last_error


@pytest.mark.asyncio
async def test_v2_exit_order_is_canceled_when_limit_net_falls_below_min_profit(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", PAPER_AUTO_FILL=False, EXIT_CONFIRM_MS=0)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    client.set_quote(Decimal("100"), Decimal("100.1"))
    mt4_tick(mt4, "99", "99.2")
    await executor.step(exit_plan("3.2"))
    assert run.state == StrategyState.QUOTING_BINANCE_EXIT
    assert executor.active_order is not None

    mt4_tick(mt4, "97.5", "97.7")
    await executor.step(exit_plan("3.2"))

    assert run.state == StrategyState.PAIR_OPEN
    assert executor.active_order is None
    events = Storage(cfg.sqlite_path).get_events(0, utc_now_ms() + 1_000, limit=10)
    assert any(event["kind"] == "v2_order_canceled" and "平仓利润保护" in event["payload"].get("reason", "") for event in events)


@pytest.mark.asyncio
async def test_v2_exit_order_uses_full_plan_net_when_rechecking_limit_profit(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", PAPER_AUTO_FILL=False, EXIT_CONFIRM_MS=0)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    store = Storage(cfg.sqlite_path)
    executor = GoldV2Executor(cfg, client, mt4, store, run)
    client.set_quote(Decimal("100"), Decimal("100.1"))
    mt4_tick(mt4, "99", "99.2")

    await executor.step(exit_plan("3.2", estimated_net="2"))
    assert run.state == StrategyState.QUOTING_BINANCE_EXIT
    assert executor.active_order is not None

    await executor.step(
        {
            "selected_entry": {"ready": False},
            "exit_plan": {
                "enabled": True,
                "target_exit_spread": "3.2",
                "estimated_net": "0.6",
                "current_exit_spread": "0.5",
            },
        }
    )

    assert run.state == StrategyState.PAIR_OPEN
    assert executor.active_order is None
    events = store.get_events(0, utc_now_ms() + 1_000, limit=20)
    canceled = [event for event in events if event["kind"] == "v2_order_canceled"]
    assert canceled
    assert "限价复算净利 0.2 低于最低 0.5" in canceled[-1]["payload"]["reason"]


@pytest.mark.asyncio
async def test_v2_live_cancels_unfilled_exit_order_when_mt4_becomes_untradable(tmp_path):
    cfg = settings(
        tmp_path,
        SQLITE_PATH=tmp_path / "test.sqlite3",
        PAPER_MODE=False,
        LIVE_TRADING=True,
        PAPER_AUTO_FILL=False,
        EXIT_CONFIRM_MS=0,
    )
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    client.set_quote(Decimal("100"), Decimal("100.1"))
    mt4_tick(
        mt4,
        "99",
        "99.2",
        symbol_trade_allowed=True,
        terminal_trade_allowed=True,
        trade_context_busy=False,
        ea_version=REQUIRED_MT4_EA_VERSION,
    )

    await executor.step(exit_plan("3.2"))

    assert run.state == StrategyState.QUOTING_BINANCE_EXIT
    assert executor.active_order is not None

    mt4_tick(
        mt4,
        "99",
        "99.2",
        symbol_trade_allowed=True,
        terminal_trade_allowed=False,
        trade_context_busy=False,
        ea_version=REQUIRED_MT4_EA_VERSION,
    )
    await executor.step(exit_plan("3.2"))

    assert run.state == StrategyState.PAIR_OPEN
    assert executor.active_order is None
    assert "禁止币安平仓" in run.last_error
    assert client._orders
    assert list(client._orders.values())[-1].status == OrderStatus.CANCELED


@pytest.mark.asyncio
async def test_v2_loss_limit_places_resting_exit_before_spread_recovers(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", PAPER_AUTO_FILL=False)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    client.set_quote(Decimal("104"), Decimal("104.2"))
    mt4_tick(mt4, "100", "100.2")

    await executor.step(
        {
            "selected_entry": {"ready": False},
            "exit_plan": {
                "enabled": True,
                "target_exit_spread": "3",
                "loss_limit": {"active": True},
            },
        }
    )

    assert run.state == StrategyState.QUOTING_BINANCE_EXIT
    assert executor.active_order is not None
    assert executor.active_order.side == Side.BUY
    assert executor.active_order.price == Decimal("103")
    events = Storage(cfg.sqlite_path).get_events(0, utc_now_ms() + 1_000, limit=10)
    exit_events = [event for event in events if event["kind"] == "v2_exit_order"]
    assert exit_events
    assert exit_events[-1]["payload"]["exit_context"]["risk_exit_active"] is True
    assert exit_events[-1]["payload"]["exit_context"]["risk_exit_reason"] == "最大亏损触发"


@pytest.mark.asyncio
async def test_v2_stale_weak_pair_places_limit_exit_even_when_net_negative(tmp_path):
    cfg = settings(
        tmp_path,
        SQLITE_PATH=tmp_path / "test.sqlite3",
        PAPER_AUTO_FILL=False,
        OPEN_MIN_EDGE=Decimal("2.40"),
        CLOSE_PROFIT_USD_PER_OZ=Decimal("1.21"),
        AGED_CLOSE_PROFIT_USD_PER_OZ=Decimal("0.10"),
    )
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    client.set_quote(Decimal("103"), Decimal("103.2"))
    mt4_tick(mt4, "100", "100.2")

    await executor.step(
        {
            "selected_entry": {"ready": False},
            "exit_plan": {
                "enabled": True,
                "target_exit_spread": "3.2",
                "estimated_net": "-1",
                "stale_weak": {"active": True, "reason": "低质量旧仓受控释放"},
            },
        }
    )

    assert run.state == StrategyState.QUOTING_BINANCE_EXIT
    assert executor.active_order is not None
    assert executor.active_order.side == Side.BUY
    assert executor.active_order.price == Decimal("102.9")
    events = Storage(cfg.sqlite_path).get_events(0, utc_now_ms() + 1_000, limit=10)
    exit_events = [event for event in events if event["kind"] == "v2_exit_order"]
    assert exit_events[-1]["payload"]["exit_context"]["risk_exit_active"] is True
    assert exit_events[-1]["payload"]["exit_context"]["risk_exit_reason"] == "低质量旧仓受控释放"


@pytest.mark.asyncio
async def test_v2_risk_exit_order_cancels_when_risk_condition_recovers(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", PAPER_AUTO_FILL=False)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    client.set_quote(Decimal("104"), Decimal("104.2"))
    mt4_tick(mt4, "100", "100.2")

    await executor.step(
        {
            "selected_entry": {"ready": False},
            "exit_plan": {
                "enabled": True,
                "target_exit_spread": "3",
                "loss_limit": {"active": True, "reason": "最大亏损触发"},
            },
        }
    )
    assert run.state == StrategyState.QUOTING_BINANCE_EXIT
    assert executor.active_order is not None
    assert executor.active_exit_order_risk_active is True

    await executor.step(
        {
            "selected_entry": {"ready": False},
            "exit_plan": {
                "enabled": True,
                "target_exit_spread": "3",
                "estimated_net": "-0.5",
                "loss_limit": {"active": False, "reason": "当前未触发最大亏损"},
            },
        }
    )

    assert run.state == StrategyState.PAIR_OPEN
    assert executor.active_order is None
    assert executor.active_exit_order_risk_active is False
    events = Storage(cfg.sqlite_path).get_events(0, utc_now_ms() + 1_000, limit=20)
    assert any(event["kind"] == "v2_order_canceled" and "风控平仓条件已解除" in event["payload"].get("reason", "") for event in events)


@pytest.mark.asyncio
async def test_v2_regular_exit_requires_confirm_time_before_order(tmp_path, monkeypatch):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", PAPER_AUTO_FILL=False, ENTRY_CONFIRM_MS=1000)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    client.set_quote(Decimal("100"), Decimal("100.1"))
    mt4_tick(mt4, "99", "99.2")
    now = {"value": 10_000}
    monkeypatch.setattr("app.v2_executor.utc_now_ms", lambda: now["value"])

    await executor.step(exit_plan("2"))
    assert executor.active_order is None
    assert "确认中" in run.last_error

    now["value"] = 10_500
    await executor.step(exit_plan("2"))
    assert executor.active_order is None

    now["value"] = 11_100
    await executor.step(exit_plan("2"))
    assert run.state == StrategyState.QUOTING_BINANCE_EXIT
    assert executor.active_order is not None


@pytest.mark.asyncio
async def test_v2_exit_confirm_can_be_shorter_than_entry_confirm(tmp_path, monkeypatch):
    cfg = settings(
        tmp_path,
        SQLITE_PATH=tmp_path / "test.sqlite3",
        PAPER_AUTO_FILL=False,
        ENTRY_CONFIRM_MS=1500,
        EXIT_CONFIRM_MS=400,
    )
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    client.set_quote(Decimal("100"), Decimal("100.1"))
    mt4_tick(mt4, "99", "99.2")
    now = {"value": 10_000}
    monkeypatch.setattr("app.v2_executor.utc_now_ms", lambda: now["value"])

    await executor.step(exit_plan("2"))
    assert executor.active_order is None

    now["value"] = 10_450
    await executor.step(exit_plan("2"))

    assert run.state == StrategyState.QUOTING_BINANCE_EXIT
    assert executor.active_order is not None


@pytest.mark.asyncio
async def test_v2_loss_limit_exit_skips_confirm_time(tmp_path, monkeypatch):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", PAPER_AUTO_FILL=False, ENTRY_CONFIRM_MS=1000)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    client.set_quote(Decimal("104"), Decimal("104.2"))
    mt4_tick(mt4, "100", "100.2")
    monkeypatch.setattr("app.v2_executor.utc_now_ms", lambda: 10_000)

    await executor.step(
        {
            "selected_entry": {"ready": False},
            "exit_plan": {
                "enabled": True,
                "target_exit_spread": "3",
                "loss_limit": {"active": True},
            },
        }
    )

    assert run.state == StrategyState.QUOTING_BINANCE_EXIT
    assert executor.active_order is not None


@pytest.mark.asyncio
async def test_v2_loss_limit_requires_risk_confirm_time(tmp_path, monkeypatch):
    cfg = settings(
        tmp_path,
        SQLITE_PATH=tmp_path / "test.sqlite3",
        PAPER_AUTO_FILL=False,
        RISK_EXIT_CONFIRM_MS=800,
    )
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    client.set_quote(Decimal("104"), Decimal("104.2"))
    mt4_tick(mt4, "100", "100.2")
    now = {"value": 10_000}
    monkeypatch.setattr("app.v2_executor.utc_now_ms", lambda: now["value"])
    loss_plan = {
        "selected_entry": {"ready": False},
        "exit_plan": {
            "enabled": True,
            "target_exit_spread": "3",
            "loss_limit": {"active": True, "reason": "最大亏损触发"},
        },
    }

    await executor.step(loss_plan)
    assert run.state == StrategyState.PAIR_OPEN
    assert executor.active_order is None
    assert "风控平仓确认中" in run.last_error

    now["value"] = 10_500
    await executor.step(
        {
            "selected_entry": {"ready": False},
            "exit_plan": {
                "enabled": True,
                "target_exit_spread": "3",
                "loss_limit": {"active": False, "reason": "当前未触发最大亏损"},
            },
        }
    )
    assert run.last_error is None
    assert executor.risk_exit_ready_since_ms == 0

    now["value"] = 11_000
    await executor.step(loss_plan)
    assert executor.active_order is None

    now["value"] = 11_500
    await executor.step(loss_plan)
    assert executor.active_order is None

    now["value"] = 11_850
    await executor.step(loss_plan)
    assert run.state == StrategyState.QUOTING_BINANCE_EXIT
    assert executor.active_order is not None


@pytest.mark.asyncio
async def test_v2_negative_swap_exit_skips_profit_guard_and_confirm_time(tmp_path, monkeypatch):
    cfg = settings(
        tmp_path,
        SQLITE_PATH=tmp_path / "test.sqlite3",
        PAPER_AUTO_FILL=False,
        ENTRY_CONFIRM_MS=1000,
        CLOSE_PROFIT_USD_PER_OZ=Decimal("1"),
    )
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    client.set_quote(Decimal("104"), Decimal("104.2"))
    mt4_tick(mt4, "100", "100.2")
    monkeypatch.setattr("app.v2_executor.utc_now_ms", lambda: 10_000)

    await executor.step(
        {
            "selected_entry": {"ready": False},
            "exit_plan": {
                "enabled": True,
                "target_exit_spread": "3",
                "estimated_net": "-0.5",
                "negative_swap": {"active": True},
            },
        }
    )

    assert run.state == StrategyState.QUOTING_BINANCE_EXIT
    assert executor.active_order is not None
    assert executor.active_order.side == Side.BUY
    assert executor.active_order.price == Decimal("103")


@pytest.mark.asyncio
async def test_v2_exit_confirm_message_clears_when_spread_no_longer_ready(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", PAPER_AUTO_FILL=False, ENTRY_CONFIRM_MS=1000)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.last_error = "V2 平仓价差已触发，确认中 784/1500ms，避免瞬时跳价假触发"
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    client.set_quote(Decimal("104"), Decimal("104.2"))
    mt4_tick(mt4, "100", "100.2")

    await executor.step(exit_plan("1"))

    assert run.state == StrategyState.PAIR_OPEN
    assert executor.active_order is None
    assert run.last_error is None


@pytest.mark.asyncio
async def test_v2_missing_exit_order_is_treated_as_canceled(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", PAPER_AUTO_FILL=False)
    client = MissingOrderBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.QUOTING_BINANCE_EXIT
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    executor.active_order = OrderUpdate(
        order_id="missing_exit_order",
        client_order_id="client_missing_exit",
        symbol="XAUUSDT",
        side=Side.BUY,
        status=OrderStatus.NEW,
        price=Decimal("100"),
        orig_qty=Decimal("1"),
        reduce_only=True,
    )

    await executor.step({"selected_entry": {"ready": False}, "exit_plan": {"enabled": True, "target_exit_spread": "2"}})

    assert run.state == StrategyState.PAIR_OPEN
    assert executor.active_order is None
    assert run.last_error is None


@pytest.mark.asyncio
async def test_v2_partial_entry_fill_does_not_queue_mt4(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", MAX_HEDGE_DELAY_MS=10_000)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)

    client.set_quote(Decimal("100"), Decimal("100.2"))
    mt4_tick(mt4, "98.8", "99")
    await executor.step(short_plan("101"))
    assert executor.active_order is not None
    await client.simulate_fill(executor.active_order.order_id, Decimal("0.4"), Decimal("101"))
    await executor.step(short_plan("101"))

    assert executor.active_order.status == OrderStatus.PARTIALLY_FILLED
    assert mt4.next_command() == {"command": "NONE"}
    assert run.state == StrategyState.QUOTING_BINANCE_ENTRY


@pytest.mark.asyncio
async def test_v2_clears_stale_post_add_wait_message_after_cooldown(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3")
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.last_error = "补仓刚完成，等待币安仓位快照稳定后再允许平仓挂单"
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry-1",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    executor.post_add_exit_block_until_ms = utc_now_ms() - 1
    client.set_quote(Decimal("100"), Decimal("100.1"))
    mt4_tick(mt4, "99", "99.2")

    await executor.step({"exit_plan": {"enabled": True, "target_exit_spread": "0.5", "estimated_net": "2"}})

    assert run.state == StrategyState.PAIR_OPEN
    assert run.last_error is None


@pytest.mark.asyncio
async def test_v2_terminal_partial_entry_requotes_remaining_then_hedges_total(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", PAPER_AUTO_FILL=False)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)

    client.set_quote(Decimal("100"), Decimal("100.2"))
    mt4_tick(mt4, "98.8", "99")
    await executor.step(short_plan("101"))
    first_order_id = executor.active_order.order_id
    await client.simulate_fill(first_order_id, Decimal("0.4"), Decimal("101"))
    client._orders[first_order_id] = client._orders[first_order_id].model_copy(update={"status": OrderStatus.CANCELED})

    await executor.step(short_plan("101"))

    assert run.state == StrategyState.QUOTING_BINANCE_ENTRY
    assert executor.active_order is not None
    assert executor.active_order.order_id != first_order_id
    assert executor.active_order.orig_qty == Decimal("0.6")
    assert mt4.next_command() == {"command": "NONE"}

    await client.simulate_fill(executor.active_order.order_id, Decimal("0.6"), Decimal("101"))
    await executor.step(short_plan("101"))
    command = mt4.next_command()
    assert command["action"] == "BUY"
    assert command["lots"] == "0.01"

    mt4.submit_report(
        Mt4Report(
            command_id=command["command_id"],
            status="ok",
            action="BUY",
            ticket=17,
            fill_price=Decimal("99"),
            lots=Decimal("0.01"),
        )
    )
    await executor.step(short_plan("101"))
    assert run.state == StrategyState.PAIR_OPEN
    assert run.open_pair.quantity_oz == Decimal("1.0")
    assert run.open_pair.binance_entry_price == Decimal("101")


@pytest.mark.asyncio
async def test_v2_mt4_hedge_timeout_waits_pending_command_without_duplicate(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3")
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)

    client.set_quote(Decimal("100"), Decimal("100.2"))
    mt4_tick(mt4, "98.8", "99")
    await executor.step(short_plan("101"))
    client.set_quote(Decimal("101"), Decimal("101.2"))
    await executor.step(short_plan("101"))
    first_command = mt4.next_command()
    assert first_command["action"] == "BUY"

    executor.hedge_started_ms = utc_now_ms() - 5_000
    await executor.step(short_plan("101"))

    assert run.state == StrategyState.HEDGING_MT4
    assert "不重复下发" in run.last_error
    assert mt4.next_command() == {"command": "NONE"}


@pytest.mark.asyncio
async def test_v2_mt4_hedge_failure_retries_after_report_without_pause(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3")
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)

    client.set_quote(Decimal("100"), Decimal("100.2"))
    mt4_tick(mt4, "98.8", "99")
    await executor.step(short_plan("101"))
    client.set_quote(Decimal("101"), Decimal("101.2"))
    await executor.step(short_plan("101"))
    failed_command = mt4.next_command()
    mt4.submit_report(
        Mt4Report(
            command_id=failed_command["command_id"],
            status="error",
            action="BUY",
            error_code=129,
            message="price changed",
        )
    )

    await executor.step(short_plan("101"))

    retry_command = mt4.next_command()
    assert run.state == StrategyState.HEDGING_MT4
    assert retry_command["action"] == "BUY"
    assert retry_command["command_id"] != failed_command["command_id"]
    assert "继续重试" in run.last_error


def test_v2_recovers_missing_entry_context_from_filled_order(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3")
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    order = OrderUpdate(
        order_id="filled-entry",
        client_order_id="client-entry",
        symbol="XAUUSDT",
        side=Side.SELL,
        status=OrderStatus.FILLED,
        price=Decimal("101"),
        orig_qty=Decimal("1"),
        executed_qty=Decimal("1"),
        avg_price=Decimal("101"),
    )

    executor._queue_mt4_add_or_entry(order)

    command = mt4.next_command()
    assert run.state == StrategyState.HEDGING_MT4
    assert command["action"] == "BUY"
    assert executor.entry_direction == "BINANCE_SHORT_MT4_LONG"
    assert executor.entry_hedge_side == Side.BUY


@pytest.mark.asyncio
async def test_v2_mt4_close_timeout_waits_pending_command_without_duplicate(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3")
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    mt4.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            bid=Decimal("99"),
            ask=Decimal("99.2"),
            positions=[
                Mt4Position(
                    ticket=7,
                    symbol="XAUUSD",
                    side=Side.BUY,
                    lots=Decimal("0.01"),
                    open_price=Decimal("99"),
                )
            ],
        )
    )

    client.set_quote(Decimal("100"), Decimal("100.1"))
    await executor.step({"selected_entry": {"ready": False}, "exit_plan": {"enabled": True, "target_exit_spread": "2", "estimated_net": "2"}})
    client.set_quote(Decimal("99.8"), executor.active_order.price)
    await executor.step({"selected_entry": {"ready": False}, "exit_plan": {"enabled": True, "target_exit_spread": "2", "estimated_net": "2"}})
    close_command = mt4.next_command()
    assert close_command["action"] == "CLOSE"

    executor.close_started_ms = utc_now_ms() - 5_000
    await executor.step({"selected_entry": {"ready": False}})

    assert run.state == StrategyState.CLOSING_MT4
    assert "不重复下发" in run.last_error
    assert mt4.next_command() == {"command": "NONE"}


@pytest.mark.asyncio
async def test_v2_mt4_close_failure_retries_after_report_without_pause(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3")
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    mt4.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            bid=Decimal("99"),
            ask=Decimal("99.2"),
            positions=[
                Mt4Position(
                    ticket=7,
                    symbol="XAUUSD",
                    side=Side.BUY,
                    lots=Decimal("0.01"),
                    open_price=Decimal("99"),
                )
            ],
        )
    )

    client.set_quote(Decimal("100"), Decimal("100.1"))
    await executor.step({"selected_entry": {"ready": False}, "exit_plan": {"enabled": True, "target_exit_spread": "2", "estimated_net": "2"}})
    client.set_quote(Decimal("99.8"), executor.active_order.price)
    await executor.step({"selected_entry": {"ready": False}, "exit_plan": {"enabled": True, "target_exit_spread": "2", "estimated_net": "2"}})
    failed_command = mt4.next_command()
    mt4.submit_report(
        Mt4Report(
            command_id=failed_command["command_id"],
            status="error",
            action="CLOSE",
            ticket=7,
            error_code=136,
            message="off quotes",
        )
    )

    await executor.step({"selected_entry": {"ready": False}})

    retry_command = mt4.next_command()
    assert run.state == StrategyState.CLOSING_MT4
    assert retry_command["action"] == "CLOSE"
    assert retry_command["ticket"] == 7
    assert retry_command["command_id"] != failed_command["command_id"]
    assert "重新发送" in run.last_error


def test_v2_partial_binance_exit_never_closes_full_mt4_ticket(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3")
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    mt4.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            bid=Decimal("99"),
            ask=Decimal("99.2"),
            positions=[
                Mt4Position(
                    ticket=7,
                    symbol="XAUUSD",
                    side=Side.BUY,
                    lots=Decimal("0.01"),
                    open_price=Decimal("99"),
                )
            ],
        )
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    partial_exit = OrderUpdate(
        order_id="partial-exit",
        client_order_id="arb_exit_partial",
        symbol="XAUUSDT",
        side=Side.BUY,
        status=OrderStatus.FILLED,
        price=Decimal("100"),
        orig_qty=Decimal("0.684"),
        executed_qty=Decimal("0.684"),
        avg_price=Decimal("100"),
        reduce_only=True,
    )

    executor._queue_mt4_close(partial_exit)

    assert mt4.next_command() == {"command": "NONE"}
    assert run.state == StrategyState.PAIR_OPEN
    assert "禁止全平 MT4" in run.last_error


@pytest.mark.asyncio
async def test_v2_binance_restore_fill_does_not_queue_mt4_follow(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", PAPER_AUTO_FILL=False)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry_order",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("2"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)
    client.set_quote(Decimal("100"), Decimal("100.2"))
    order = await client.place_post_only_order(
        OrderRequest(
            symbol="XAUUSDT",
            side=Side.SELL,
            quantity=Decimal("0.316"),
            price=Decimal("101"),
            client_order_id="arb_restore_test",
            position_side="SHORT",
        )
    )
    executor.start_binance_restore(order, Decimal("0.684"))
    await client.simulate_fill(order.order_id, Decimal("0.316"), Decimal("101"))

    await executor.step({})

    assert mt4.next_command() == {"command": "NONE"}
    assert run.state == StrategyState.PAIR_OPEN
    assert run.open_pair is not None
    assert run.open_pair.binance_entry_price == Decimal("101")
    assert executor.repairing_binance_only is False


@pytest.mark.asyncio
async def test_v2_add_position_merges_pair_after_binance_fill_and_mt4_follow(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", MAX_ADD_COUNT=1)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)

    client.set_quote(Decimal("100"), Decimal("100.2"))
    mt4_tick(mt4, "98.8", "99")
    await executor.step(short_plan("101"))
    client.set_quote(Decimal("101"), Decimal("101.2"))
    await executor.step(short_plan("101"))
    command = mt4.next_command()
    mt4.submit_report(Mt4Report(command_id=command["command_id"], status="ok", action="BUY", ticket=7, fill_price=Decimal("99"), lots=Decimal("0.01")))
    await executor.step(short_plan("101"))

    client.set_quote(Decimal("104"), Decimal("104.2"))
    mt4_tick(mt4, "100.8", "101")
    await executor.step(add_plan("105", actionable="3.8"))
    assert run.state == StrategyState.QUOTING_BINANCE_ENTRY
    assert executor.adding_to_pair is True

    client.set_quote(Decimal("105"), Decimal("105.2"))
    await executor.step(add_plan("105"))
    add_command = mt4.next_command()
    assert add_command["action"] == "BUY"
    assert add_command["reason"] == "v2_add_follow"

    mt4.submit_report(Mt4Report(command_id=add_command["command_id"], status="ok", action="BUY", ticket=8, fill_price=Decimal("101.5"), lots=Decimal("0.01")))
    await executor.step(add_plan("105", actionable="3.8"))
    assert run.state == StrategyState.PAIR_OPEN
    assert run.open_pair.quantity_oz == Decimal("2")
    assert run.open_pair.add_count == 1
    assert run.open_pair.last_add_trigger_edge == Decimal("3.8")
    assert run.open_pair.mt4_tickets == [7, 8]

    client.set_quote(Decimal("101"), Decimal("101.2"))
    mt4_tick(mt4, "101", "101.2")
    await executor.step({"selected_entry": {"ready": False}, "exit_plan": {"enabled": True, "target_exit_spread": "10", "estimated_net": "2"}})
    assert run.state == StrategyState.PAIR_OPEN
    assert executor.active_order is None


@pytest.mark.asyncio
async def test_v2_add_position_blocks_future_adds_when_actual_edge_degrades_average(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", MAX_ADD_COUNT=3)
    store = Storage(cfg.sqlite_path)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    executor = GoldV2Executor(cfg, client, mt4, store, run)

    client.set_quote(Decimal("100"), Decimal("100.2"))
    mt4_tick(mt4, "98.8", "99")
    await executor.step(short_plan("101"))
    client.set_quote(Decimal("101"), Decimal("101.2"))
    await executor.step(short_plan("101"))
    command = mt4.next_command()
    mt4.submit_report(
        Mt4Report(
            command_id=command["command_id"],
            status="ok",
            action="BUY",
            ticket=7,
            fill_price=Decimal("99"),
            lots=Decimal("0.01"),
        )
    )
    await executor.step(short_plan("101"))

    await executor.step(add_plan("105", actionable="3.8"))
    client.set_quote(Decimal("105"), Decimal("105.2"))
    await executor.step(add_plan("105"))
    add_command = mt4.next_command()
    mt4.submit_report(
        Mt4Report(
            command_id=add_command["command_id"],
            status="ok",
            action="BUY",
            ticket=8,
            fill_price=Decimal("104"),
            lots=Decimal("0.01"),
        )
    )
    await executor.step(add_plan("105", actionable="3.8"))

    assert run.open_pair.add_count == cfg.max_add_count
    assert run.open_pair.last_add_edge == Decimal("1")
    events = store.get_events(0, 4_102_444_800_000)
    assert any(event["kind"] == "v2_add_degraded_average_blocked" for event in events)


@pytest.mark.asyncio
async def test_v2_add_position_waits_for_confirm_before_order(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", MAX_ADD_COUNT=1, ENTRY_CONFIRM_MS=1500)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("103"),
        mt4_entry_price=Decimal("100"),
        binance_order_id="entry",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("3"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)

    client.set_quote(Decimal("104"), Decimal("104.2"))
    mt4_tick(mt4, "100.8", "101")
    await executor.step(add_plan("105"))

    assert run.state == StrategyState.PAIR_OPEN
    assert executor.active_order is None
    assert executor.add_ready_since_ms > 0
    assert run.last_error.startswith("V2 补仓价差已触发，确认中")

    executor.add_ready_since_ms = utc_now_ms() - cfg.entry_confirm_ms
    await executor.step(add_plan("105"))

    assert run.state == StrategyState.QUOTING_BINANCE_ENTRY
    assert executor.active_order is not None
    assert executor.adding_to_pair is True
    assert run.last_error is None


@pytest.mark.asyncio
async def test_v2_add_confirm_survives_temporary_safety_block(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", MAX_ADD_COUNT=1, ENTRY_CONFIRM_MS=1500)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("103"),
        mt4_entry_price=Decimal("100"),
        binance_order_id="entry",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("3"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)

    await executor.step(add_plan("105"))
    assert executor.add_ready_since_ms > 0
    executor.add_ready_since_ms = utc_now_ms() - cfg.entry_confirm_ms

    await executor.step(add_plan_temporarily_blocked_after_trigger())

    assert executor.add_ready_since_ms > 0
    assert run.state == StrategyState.PAIR_OPEN
    assert run.last_error == "补仓后仍不安全"

    await executor.step(add_plan("105"))

    assert run.state == StrategyState.QUOTING_BINANCE_ENTRY
    assert executor.active_order is not None
    assert executor.adding_to_pair is True


@pytest.mark.asyncio
async def test_v2_add_confirm_waits_for_actionable_trigger_and_quotes_protected_price(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", MAX_ADD_COUNT=1, ENTRY_CONFIRM_MS=1500)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    run.state = StrategyState.PAIR_OPEN
    run.open_pair = OpenPair(
        direction="BINANCE_SHORT_MT4_LONG",
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("103"),
        mt4_entry_price=Decimal("100"),
        binance_order_id="entry",
        mt4_ticket=7,
        mt4_tickets=[7],
        base_edge=Decimal("3"),
    )
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)

    await executor.step(add_plan_reached_base_trigger_before_actionable_price())

    assert executor.add_ready_since_ms == 0
    assert run.state == StrategyState.PAIR_OPEN
    assert executor.active_order is None
    assert run.last_error is None

    await executor.step(add_plan_actionable_after_base_trigger())
    assert executor.add_ready_since_ms > 0
    assert run.state == StrategyState.PAIR_OPEN
    assert executor.active_order is None
    assert run.last_error.startswith("V2 补仓价差已触发，确认中")

    executor.add_ready_since_ms = utc_now_ms() - cfg.entry_confirm_ms
    await executor.step(add_plan_actionable_after_base_trigger())

    assert run.state == StrategyState.QUOTING_BINANCE_ENTRY
    assert executor.active_order is not None
    assert executor.adding_to_pair is True
    assert executor.active_order.price == Decimal("105")


@pytest.mark.asyncio
async def test_v2_exit_closes_all_mt4_tickets_before_clearing_pair(tmp_path):
    cfg = settings(tmp_path, SQLITE_PATH=tmp_path / "test.sqlite3", MAX_ADD_COUNT=1)
    client = PaperBinanceClient(cfg)
    mt4 = Mt4Bridge(cfg)
    run = runtime()
    executor = GoldV2Executor(cfg, client, mt4, Storage(cfg.sqlite_path), run)

    client.set_quote(Decimal("100"), Decimal("100.2"))
    mt4_tick(mt4, "98.8", "99")
    await executor.step(short_plan("101"))
    client.set_quote(Decimal("101"), Decimal("101.2"))
    await executor.step(short_plan("101"))
    command = mt4.next_command()
    mt4.submit_report(
        Mt4Report(
            command_id=command["command_id"],
            status="ok",
            action="BUY",
            ticket=7,
            fill_price=Decimal("99"),
            lots=Decimal("0.01"),
        )
    )
    await executor.step(short_plan("101"))

    client.set_quote(Decimal("104"), Decimal("104.2"))
    mt4_tick(mt4, "100.8", "101")
    await executor.step(add_plan("105"))
    client.set_quote(Decimal("105"), Decimal("105.2"))
    await executor.step(add_plan("105"))
    add_command = mt4.next_command()
    mt4.submit_report(
        Mt4Report(
            command_id=add_command["command_id"],
            status="ok",
            action="BUY",
            ticket=8,
            fill_price=Decimal("101.5"),
            lots=Decimal("0.01"),
        )
    )
    await executor.step(add_plan("105"))

    executor.post_add_exit_block_until_ms = 0
    mt4.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            bid=Decimal("100.8"),
            ask=Decimal("101.1"),
            positions=[],
        )
    )
    client.set_quote(Decimal("101"), Decimal("101.2"))
    await executor.step({"selected_entry": {"ready": False}, "exit_plan": {"enabled": True, "target_exit_spread": "10", "estimated_net": "2"}})
    assert run.state == StrategyState.QUOTING_BINANCE_EXIT

    client.set_quote(Decimal("100.8"), executor.active_order.price)
    await executor.step({"selected_entry": {"ready": False}, "exit_plan": {"enabled": True, "target_exit_spread": "10", "estimated_net": "2"}})
    first_close = mt4.next_command()
    second_close = mt4.next_command()
    assert {first_close["ticket"], second_close["ticket"]} == {7, 8}

    mt4.submit_report(
        Mt4Report(
            command_id=first_close["command_id"],
            status="ok",
            action="CLOSE",
            ticket=first_close["ticket"],
            fill_price=Decimal("100.8"),
            lots=Decimal("0.01"),
        )
    )
    await executor.step({"selected_entry": {"ready": False}})
    assert run.state == StrategyState.CLOSING_MT4
    assert run.open_pair is not None

    mt4.submit_report(
        Mt4Report(
            command_id=second_close["command_id"],
            status="ok",
            action="CLOSE",
            ticket=second_close["ticket"],
            fill_price=Decimal("100.8"),
            lots=Decimal("0.01"),
        )
    )
    await executor.step({"selected_entry": {"ready": False}})
    assert run.state == StrategyState.IDLE
    assert run.open_pair is None
