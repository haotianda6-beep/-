from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.binance_client import BinanceError, PaperBinanceClient
from app.config import Settings
from app.models import Mt4Position, Mt4Report, Mt4Tick, OpenPair, OrderStatus, OrderUpdate, Side, StrategyState, utc_now_ms
from app.mt4_bridge import Mt4Bridge
from app.storage import Storage
from app.v2_executor import GoldV2Executor


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
        "CLOSE_PROFIT_USD_PER_OZ": Decimal("0.5"),
        **kwargs,
    }
    return Settings(_env_file=None, **values)


def runtime():
    return SimpleNamespace(state=StrategyState.IDLE, last_error=None, open_pair=None)


def mt4_tick(mt4: Mt4Bridge, bid: str, ask: str) -> None:
    mt4.update_tick(Mt4Tick(symbol="XAUUSD", bid=Decimal(bid), ask=Decimal(ask)))


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


def add_plan(price: str = "105") -> dict:
    return {
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

    mt4.submit_report(Mt4Report(command_id=command["command_id"], status="ok", action="BUY", ticket=7, fill_price=Decimal("99"), lots=Decimal("0.01")))
    await executor.step(short_plan("101"))
    assert run.state == StrategyState.PAIR_OPEN
    assert run.open_pair is not None
    assert run.open_pair.base_edge == Decimal("2")

    client.set_quote(Decimal("100"), Decimal("100.1"))
    mt4_tick(mt4, "99", "99.3")
    await executor.step(short_plan("101"))
    assert run.state == StrategyState.QUOTING_BINANCE_EXIT
    assert executor.active_order is not None
    assert executor.active_order.reduce_only is True
    assert executor.active_order.side == Side.BUY

    client.set_quote(Decimal("99.8"), executor.active_order.price)
    await executor.step(short_plan("101"))
    close_command = mt4.next_command()
    assert close_command["action"] == "CLOSE"
    assert close_command["ticket"] == 7

    mt4.submit_report(Mt4Report(command_id=close_command["command_id"], status="ok", action="CLOSE", ticket=7, fill_price=Decimal("99.2"), lots=Decimal("0.01")))
    await executor.step(short_plan("101"))
    assert run.state == StrategyState.IDLE
    assert run.open_pair is None
    assert all(order.is_maker for order in client._orders.values())


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

    await executor.step({"exit_plan": {"enabled": True, "target_exit_spread": "0.5"}})

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
    await executor.step({"selected_entry": {"ready": False}, "exit_plan": {"enabled": True, "target_exit_spread": "2"}})
    client.set_quote(Decimal("99.8"), executor.active_order.price)
    await executor.step({"selected_entry": {"ready": False}, "exit_plan": {"enabled": True, "target_exit_spread": "2"}})
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
    await executor.step({"selected_entry": {"ready": False}, "exit_plan": {"enabled": True, "target_exit_spread": "2"}})
    client.set_quote(Decimal("99.8"), executor.active_order.price)
    await executor.step({"selected_entry": {"ready": False}, "exit_plan": {"enabled": True, "target_exit_spread": "2"}})
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
    await executor.step(add_plan("105"))
    assert run.state == StrategyState.QUOTING_BINANCE_ENTRY
    assert executor.adding_to_pair is True

    client.set_quote(Decimal("105"), Decimal("105.2"))
    await executor.step(add_plan("105"))
    add_command = mt4.next_command()
    assert add_command["action"] == "BUY"
    assert add_command["reason"] == "v2_add_follow"

    mt4.submit_report(Mt4Report(command_id=add_command["command_id"], status="ok", action="BUY", ticket=8, fill_price=Decimal("101.5"), lots=Decimal("0.01")))
    await executor.step(add_plan("105"))
    assert run.state == StrategyState.PAIR_OPEN
    assert run.open_pair.quantity_oz == Decimal("2")
    assert run.open_pair.add_count == 1
    assert run.open_pair.last_add_trigger_edge == Decimal("3")
    assert run.open_pair.mt4_tickets == [7, 8]

    client.set_quote(Decimal("101"), Decimal("101.2"))
    mt4_tick(mt4, "101", "101.2")
    await executor.step({"selected_entry": {"ready": False}, "exit_plan": {"enabled": True, "target_exit_spread": "10"}})
    assert run.state == StrategyState.PAIR_OPEN
    assert executor.active_order is None


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
    await executor.step({"selected_entry": {"ready": False}, "exit_plan": {"enabled": True, "target_exit_spread": "10"}})
    assert run.state == StrategyState.QUOTING_BINANCE_EXIT

    client.set_quote(Decimal("100.8"), executor.active_order.price)
    await executor.step({"selected_entry": {"ready": False}, "exit_plan": {"enabled": True, "target_exit_spread": "10"}})
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
