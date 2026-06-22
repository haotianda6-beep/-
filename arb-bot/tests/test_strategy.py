from decimal import Decimal

import pytest

from app.binance_client import BinanceError, PaperBinanceClient
from app.config import Settings
from app.models import (
    ExchangeFilters,
    MarketQuote,
    Mt4Position,
    Mt4Report,
    Mt4Tick,
    OpenPair,
    OrderStatus,
    OrderUpdate,
    PairDirection,
    Side,
    StrategyState,
    utc_now_ms,
)
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
        "ENTRY_CONFIRM_MS": 0,
        "MIN_ORDER_LIVE_MS": 0,
        "REQUOTE_COOLDOWN_MS": 0,
        "CANCEL_MIN_EDGE": Decimal("1.20"),
        "TARGET_OZ": Decimal("1"),
        "MT4_LOT_SIZE_OZ": Decimal("100"),
        **kwargs,
    }
    return Settings(_env_file=None, **values)


def filters() -> ExchangeFilters:
    return ExchangeFilters(tick_size=Decimal("0.1"), qty_step=Decimal("0.001"), min_qty=Decimal("0.001"))


def test_binance_high_entry_formula_uses_maker_offset(tmp_path):
    cfg = settings(
        tmp_path,
        OPEN_MIN_EDGE=Decimal("1.50"),
        MIN_LOCKED_EDGE=Decimal("0.80"),
        BINANCE_ENTRY_OFFSET_USD=Decimal("3"),
    )
    plan = build_entry_plan(
        cfg,
        filters(),
        MarketQuote(symbol="XAUUSDT", bid=Decimal("2001"), ask=Decimal("2002")),
        MarketQuote(symbol="XAUUSD", bid=Decimal("1999"), ask=Decimal("2000")),
    )
    assert plan is not None
    assert plan.direction == PairDirection.BINANCE_SHORT_MT4_LONG
    assert plan.binance_side == Side.SELL
    assert plan.limit_price == Decimal("2005.0")
    assert plan.mt4_hedge_side == Side.BUY
    assert plan.mt4_price_limit == Decimal("2004.2")


def test_binance_low_entry_formula_uses_maker_offset(tmp_path):
    cfg = settings(
        tmp_path,
        OPEN_MIN_EDGE=Decimal("1.50"),
        MIN_LOCKED_EDGE=Decimal("0.80"),
        BINANCE_ENTRY_OFFSET_USD=Decimal("3"),
    )
    plan = build_entry_plan(
        cfg,
        filters(),
        MarketQuote(symbol="XAUUSDT", bid=Decimal("1998"), ask=Decimal("1999")),
        MarketQuote(symbol="XAUUSD", bid=Decimal("2000"), ask=Decimal("2001")),
    )
    assert plan is not None
    assert plan.direction == PairDirection.BINANCE_LONG_MT4_SHORT
    assert plan.binance_side == Side.BUY
    assert plan.limit_price == Decimal("1995.0")
    assert plan.mt4_hedge_side == Side.SELL
    assert plan.mt4_price_limit == Decimal("1995.8")


def test_resume_paused_engine_clears_stale_entry_state(tmp_path):
    cfg = settings(tmp_path)
    engine = StrategyEngine(cfg, PaperBinanceClient(cfg), Mt4Bridge(cfg), RiskManager(cfg), Storage(cfg.sqlite_path))
    engine.state = StrategyState.PAUSED
    engine.last_error = "MT4 buy ask above max"
    engine.active_order = OrderUpdate(
        order_id="old_order",
        client_order_id="arb_old",
        symbol="XAUUSDT",
        side=Side.SELL,
        status=OrderStatus.FILLED,
        price=Decimal("4347.54"),
        orig_qty=Decimal("1"),
        executed_qty=Decimal("1"),
    )

    engine.resume()

    assert engine.state == StrategyState.IDLE
    assert engine.last_error is None
    assert engine.active_order is None


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
    assert command["max_price"] is None
    assert command["min_price"] is None
    assert engine.state == StrategyState.HEDGING_MT4


@pytest.mark.asyncio
async def test_hedge_timeout_starts_when_mt4_command_is_queued(tmp_path):
    engine, client, mt4 = await make_engine(
        tmp_path,
        settings_kwargs={"MAX_HEDGE_DELAY_MS": 800, "MAX_ORDER_AGE_MS": 5000, "MIN_ORDER_LIVE_MS": 5000},
    )
    await engine.step()
    order = engine.active_order
    assert order is not None
    engine.order_created_ms = utc_now_ms() - 5000
    await client.simulate_fill(order.order_id, Decimal("1"), Decimal("2002"))

    await engine.step()
    command = mt4.next_command()
    assert command["action"] == "BUY"
    assert engine.state == StrategyState.HEDGING_MT4
    assert engine.hedge_started_ms > engine.order_created_ms

    await engine.step()

    assert engine.state == StrategyState.HEDGING_MT4
    emergency = [item for item in client._orders.values() if item.is_maker is False and item.reduce_only]
    assert emergency == []


@pytest.mark.asyncio
async def test_unfilled_entry_order_cancels_when_spread_no_longer_valid(tmp_path):
    engine, client, mt4 = await make_engine(tmp_path)
    await engine.step()
    order = engine.active_order
    assert order is not None

    client.set_quote(Decimal("2000.4"), Decimal("2000.5"))
    await engine.step()

    assert engine.state == StrategyState.IDLE
    assert engine.active_order is None
    canceled = await client.get_order(order.order_id)
    assert canceled is not None
    assert canceled.status == OrderStatus.CANCELED


@pytest.mark.asyncio
async def test_entry_requires_confirm_time_before_order(tmp_path):
    engine, client, mt4 = await make_engine(tmp_path, settings_kwargs={"ENTRY_CONFIRM_MS": 1000})

    await engine.step()

    assert engine.state == StrategyState.IDLE
    assert engine.active_order is None
    assert engine.candidate_plan is not None

    engine.candidate_started_ms -= 1000
    await engine.step()

    assert engine.state == StrategyState.QUOTING_BINANCE_ENTRY
    assert engine.active_order is not None


@pytest.mark.asyncio
async def test_cancel_hysteresis_keeps_order_until_cancel_edge_breaks(tmp_path):
    engine, client, mt4 = await make_engine(
        tmp_path,
        settings_kwargs={
            "OPEN_MIN_EDGE": Decimal("2.00"),
            "CANCEL_MIN_EDGE": Decimal("1.70"),
            "MIN_ORDER_LIVE_MS": 0,
        },
    )
    await engine.step()
    order = engine.active_order
    assert order is not None

    client.set_quote(Decimal("2001.7"), Decimal("2001.8"))
    await engine.step()

    assert engine.state == StrategyState.QUOTING_BINANCE_ENTRY
    assert engine.active_order is not None
    still_open = await client.get_order(order.order_id)
    assert still_open is not None
    assert still_open.status == OrderStatus.NEW

    client.set_quote(Decimal("2001.5"), Decimal("2001.6"))
    await engine.step()

    assert engine.state == StrategyState.IDLE
    canceled = await client.get_order(order.order_id)
    assert canceled is not None
    assert canceled.status == OrderStatus.CANCELED


@pytest.mark.asyncio
async def test_requote_cooldown_blocks_immediate_reentry_after_cancel(tmp_path):
    engine, client, mt4 = await make_engine(
        tmp_path,
        settings_kwargs={"REQUOTE_COOLDOWN_MS": 2000, "CANCEL_MIN_EDGE": Decimal("1.20")},
    )
    await engine.step()
    order = engine.active_order
    assert order is not None

    client.set_quote(Decimal("2000.4"), Decimal("2000.5"))
    await engine.step()
    assert engine.state == StrategyState.IDLE

    client.set_quote(Decimal("2001"), Decimal("2002"))
    await engine.step()
    assert engine.active_order is None

    engine.last_entry_cancel_ms -= 2000
    await engine.step()

    assert engine.state == StrategyState.QUOTING_BINANCE_ENTRY
    assert engine.active_order is not None
    assert engine.active_order.order_id != order.order_id


@pytest.mark.asyncio
async def test_entry_order_temporarily_not_visible_keeps_tracking_original_order(tmp_path):
    engine, client, mt4 = await make_engine(tmp_path, NotVisibleOncePaperClient)
    await engine.step()
    first_order = engine.active_order
    assert first_order is not None

    await engine.step()

    assert engine.state == StrategyState.QUOTING_BINANCE_ENTRY
    assert engine.active_order is not None
    assert engine.active_order.order_id == first_order.order_id
    assert len([order for order in client._orders.values() if order.status == OrderStatus.NEW]) == 1


@pytest.mark.asyncio
async def test_live_entry_guard_blocks_when_orphan_binance_order_exists(tmp_path):
    engine, client, mt4 = await make_engine(tmp_path)
    client._orders["orphan"] = OrderUpdate(
        order_id="orphan",
        client_order_id="arb_orphan",
        symbol=client.settings.binance_symbol,
        side=Side.SELL,
        status=OrderStatus.NEW,
        price=Decimal("2005"),
        orig_qty=Decimal("1"),
    )

    await engine.step()

    assert engine.state == StrategyState.PAUSED
    assert engine.active_order is None
    live_orders = [order for order in client._orders.values() if order.status == OrderStatus.NEW]
    assert len(live_orders) == 1
    assert live_orders[0].order_id == "orphan"


@pytest.mark.asyncio
async def test_live_entry_guard_blocks_when_mt4_position_exists(tmp_path):
    engine, client, mt4 = await make_engine(tmp_path)
    mt4.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            bid=Decimal("1999"),
            ask=Decimal("2000"),
            positions=[
                Mt4Position(
                    ticket=123,
                    symbol="XAUUSD",
                    side=Side.BUY,
                    lots=Decimal("0.01"),
                    open_price=Decimal("2000"),
                )
            ],
            account_margin=Decimal("4.34"),
        )
    )

    await engine.step()

    assert engine.state == StrategyState.PAUSED
    assert engine.active_order is None
    assert "MT4" in (engine.last_error or "")


@pytest.mark.asyncio
async def test_cancel_race_fill_queues_mt4_hedge(tmp_path):
    engine, client, mt4 = await make_engine(tmp_path, FillOnCancelPaperClient)
    await engine.step()
    order = engine.active_order
    assert order is not None

    client.set_quote(Decimal("2000.4"), Decimal("2000.5"))
    await engine.step()

    command = mt4.next_command()
    assert command["action"] == "BUY"
    assert Decimal(str(command["lots"])) == Decimal("0.01")
    assert engine.state == StrategyState.HEDGING_MT4


@pytest.mark.asyncio
async def test_stale_quote_cancel_race_fill_emergency_closes_binance(tmp_path):
    engine, client, mt4 = await make_engine(tmp_path, FillOnCancelPaperClient)
    await engine.step()
    order = engine.active_order
    assert order is not None

    engine.last_error = "quote stale 3000ms"
    await engine._cancel_stale_entry_quote()

    emergency = [item for item in client._orders.values() if item.is_maker is False and item.reduce_only]
    assert emergency
    assert emergency[-1].side == Side.BUY
    assert emergency[-1].executed_qty == Decimal("1")
    assert engine.state == StrategyState.PAUSED


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
    assert engine.active_order is None
    assert engine.active_plan is None


@pytest.mark.asyncio
async def test_exit_closes_mt4_ticket_instead_of_opening_reverse_order(tmp_path):
    engine, client, mt4 = await make_engine(tmp_path)
    await engine.step()
    entry_order = engine.active_order
    assert entry_order is not None
    await client.simulate_fill(entry_order.order_id, Decimal("1"), Decimal("2002"))
    await engine.step()
    entry_command = mt4.next_command()
    mt4.submit_report(
        Mt4Report(
            command_id=entry_command["command_id"],
            status="ok",
            action="BUY",
            ticket=123456,
            fill_price=Decimal("2000"),
            lots=Decimal("0.01"),
        )
    )
    await engine.step()
    assert engine.state == StrategyState.PAIR_OPEN

    client.set_quote(Decimal("2000.0"), Decimal("2000.1"))
    mt4.update_tick(Mt4Tick(symbol="XAUUSD", bid=Decimal("2000.0"), ask=Decimal("2000.1")))
    await engine.step()
    exit_order = engine.active_order
    assert exit_order is not None
    await client.simulate_fill(exit_order.order_id, Decimal("1"), Decimal("2000"))
    await engine.step()

    close_command = mt4.next_command()
    assert close_command["action"] == "CLOSE"
    assert close_command["ticket"] == 123456
    assert Decimal(str(close_command["lots"])) == Decimal("0.01")


@pytest.mark.asyncio
async def test_exit_order_cancels_when_spread_widens_before_fill(tmp_path):
    engine, client, mt4 = await make_engine(tmp_path)
    await engine.step()
    entry_order = engine.active_order
    assert entry_order is not None
    await client.simulate_fill(entry_order.order_id, Decimal("1"), Decimal("2002"))
    await engine.step()
    entry_command = mt4.next_command()
    mt4.submit_report(
        Mt4Report(
            command_id=entry_command["command_id"],
            status="ok",
            action="BUY",
            ticket=123456,
            fill_price=Decimal("2000"),
            lots=Decimal("0.01"),
        )
    )
    await engine.step()
    client.set_quote(Decimal("2000.0"), Decimal("2000.1"))
    mt4.update_tick(Mt4Tick(symbol="XAUUSD", bid=Decimal("2000.0"), ask=Decimal("2000.1")))
    await engine.step()
    exit_order = engine.active_order
    assert exit_order is not None

    mt4.update_tick(Mt4Tick(symbol="XAUUSD", bid=Decimal("1997.0"), ask=Decimal("1997.1")))
    await engine.step()

    canceled = await client.get_order(exit_order.order_id)
    assert canceled is not None
    assert canceled.status == OrderStatus.CANCELED
    assert engine.state == StrategyState.PAIR_OPEN
    assert engine.active_order is None
    assert mt4.next_command() == {"command": "NONE"}


@pytest.mark.asyncio
async def test_pair_open_stale_quote_waits_without_hard_pause(tmp_path):
    engine, client, mt4 = await make_engine(tmp_path, PositionTrackingPaperClient)
    await open_live_pair(engine, client, mt4, ticket=111111)
    engine.settings.max_quote_age_ms = 100
    client._quote = MarketQuote(
        symbol="XAUUSDT",
        bid=Decimal("2002"),
        ask=Decimal("2003"),
        timestamp_ms=utc_now_ms() - 1000,
    )
    mt4.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            bid=Decimal("1999"),
            ask=Decimal("2000"),
            timestamp_ms=utc_now_ms() - 1000,
            positions=[
                Mt4Position(ticket=111111, symbol="XAUUSD", side=Side.BUY, lots=Decimal("0.01"), open_price=Decimal("2000")),
            ],
        )
    )

    await engine.step()

    assert engine.state == StrategyState.PAIR_OPEN
    assert engine.active_order is None
    assert engine.last_error is not None
    assert engine.last_error.startswith("quote stale ")


@pytest.mark.asyncio
async def test_quote_pause_with_open_pair_auto_resumes_position_management(tmp_path):
    engine, client, mt4 = await make_engine(tmp_path, PositionTrackingPaperClient)
    await open_live_pair(engine, client, mt4, ticket=111111)
    engine.state = StrategyState.PAUSED
    engine.last_error = "quote stale 3000ms"
    client.set_quote(Decimal("2000.0"), Decimal("2000.1"))
    mt4.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            bid=Decimal("2000.0"),
            ask=Decimal("2000.1"),
            positions=[
                Mt4Position(ticket=111111, symbol="XAUUSD", side=Side.BUY, lots=Decimal("0.01"), open_price=Decimal("2000")),
            ],
        )
    )

    await engine.step()

    assert engine.state == StrategyState.QUOTING_BINANCE_EXIT
    assert engine.active_order is not None
    assert engine.active_order.reduce_only is True
    assert engine.last_error is None


@pytest.mark.asyncio
async def test_negative_mt4_swap_forces_exit_before_rollover(tmp_path):
    engine, client, mt4 = await make_engine(
        tmp_path,
        PositionTrackingPaperClient,
        settings_kwargs={"NEGATIVE_SWAP_CLOSE_BEFORE_MINUTES": 30},
    )
    await open_live_pair(engine, client, mt4, ticket=111111)
    client.set_quote(Decimal("2002"), Decimal("2003"))
    mt4.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            bid=Decimal("1999"),
            ask=Decimal("2000"),
            positions=[
                Mt4Position(ticket=111111, symbol="XAUUSD", side=Side.BUY, lots=Decimal("0.01"), open_price=Decimal("2000")),
            ],
            swap_long_per_lot=Decimal("-100"),
            swap_short_per_lot=Decimal("20"),
            swap_type=1,
            next_rollover_time_ms=utc_now_ms() + 20 * 60 * 1000,
        )
    )

    await engine.step()

    assert engine.state == StrategyState.QUOTING_BINANCE_EXIT
    assert engine.active_order is not None
    assert engine.active_order.reduce_only is True
    assert engine.active_order.side == Side.BUY
    assert engine.exit_force_reason is not None
    assert "隔夜费" in engine.exit_force_reason


@pytest.mark.asyncio
async def test_negative_mt4_swap_waits_until_close_window(tmp_path):
    engine, client, mt4 = await make_engine(
        tmp_path,
        PositionTrackingPaperClient,
        settings_kwargs={"NEGATIVE_SWAP_CLOSE_BEFORE_MINUTES": 30, "MAX_ADD_COUNT": 0},
    )
    await open_live_pair(engine, client, mt4, ticket=111111)
    client.set_quote(Decimal("2002"), Decimal("2003"))
    mt4.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            bid=Decimal("1999"),
            ask=Decimal("2000"),
            positions=[
                Mt4Position(ticket=111111, symbol="XAUUSD", side=Side.BUY, lots=Decimal("0.01"), open_price=Decimal("2000")),
            ],
            swap_long_per_lot=Decimal("-100"),
            swap_short_per_lot=Decimal("20"),
            swap_type=1,
            next_rollover_time_ms=utc_now_ms() + 40 * 60 * 1000,
        )
    )

    await engine.step()

    assert engine.state == StrategyState.PAIR_OPEN
    assert engine.active_order is None


@pytest.mark.asyncio
async def test_negative_mt4_swap_does_not_exit_when_convergence_still_profitable(tmp_path):
    engine, client, mt4 = await make_engine(
        tmp_path,
        PositionTrackingPaperClient,
        settings_kwargs={"NEGATIVE_SWAP_CLOSE_BEFORE_MINUTES": 30, "MAX_ADD_COUNT": 0},
    )
    await open_live_pair(engine, client, mt4, ticket=111111)
    client.set_quote(Decimal("2002"), Decimal("2003"))
    mt4.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            bid=Decimal("1999"),
            ask=Decimal("2000"),
            positions=[
                Mt4Position(ticket=111111, symbol="XAUUSD", side=Side.BUY, lots=Decimal("0.01"), open_price=Decimal("2000")),
            ],
            swap_long_per_lot=Decimal("-10"),
            swap_short_per_lot=Decimal("20"),
            swap_type=1,
            next_rollover_time_ms=utc_now_ms() + 20 * 60 * 1000,
        )
    )

    await engine.step()

    assert engine.state == StrategyState.PAIR_OPEN
    assert engine.active_order is None


@pytest.mark.asyncio
async def test_add_position_keeps_existing_direction_when_edge_grows(tmp_path):
    engine, client, mt4 = await make_engine(
        tmp_path,
        PositionTrackingPaperClient,
        settings_kwargs={"ADD_EDGE_GROWTH_USD": Decimal("1"), "MAX_ADD_COUNT": 2},
    )
    await open_live_pair(engine, client, mt4, ticket=111111)
    assert engine.open_pair is not None
    assert engine.open_pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG

    client.set_quote(Decimal("2002"), Decimal("2003"))
    mt4.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            bid=Decimal("1999"),
            ask=Decimal("2000"),
            positions=[
                Mt4Position(
                    ticket=111111,
                    symbol="XAUUSD",
                    side=Side.BUY,
                    lots=Decimal("0.01"),
                    open_price=Decimal("2000"),
                )
            ],
            account_margin=Decimal("4.34"),
        )
    )

    await engine.step()

    add_order = engine.active_order
    assert add_order is not None
    assert add_order.side == Side.SELL
    assert engine.adding_to_pair is True
    assert engine.state == StrategyState.QUOTING_BINANCE_ENTRY

    await client.simulate_fill(add_order.order_id, Decimal("1"), add_order.price)
    await engine.step()
    add_command = mt4.next_command()
    assert add_command["action"] == "BUY"

    mt4.submit_report(
        Mt4Report(
            command_id=add_command["command_id"],
            status="ok",
            action="BUY",
            ticket=222222,
            fill_price=Decimal("2000"),
            lots=Decimal("0.01"),
        )
    )
    await engine.step()

    assert engine.state == StrategyState.PAIR_OPEN
    assert engine.open_pair is not None
    assert engine.open_pair.quantity_oz == Decimal("2")
    assert engine.open_pair.add_count == 1
    assert engine.open_pair.mt4_tickets == [111111, 222222]


@pytest.mark.asyncio
async def test_exit_after_add_closes_all_mt4_tickets(tmp_path):
    engine, client, mt4 = await make_engine(
        tmp_path,
        PositionTrackingPaperClient,
        settings_kwargs={"ADD_EDGE_GROWTH_USD": Decimal("1"), "MAX_ADD_COUNT": 2},
    )
    await open_live_pair(engine, client, mt4, ticket=111111)
    client.set_quote(Decimal("2002"), Decimal("2003"))
    mt4.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            bid=Decimal("1999"),
            ask=Decimal("2000"),
            positions=[
                Mt4Position(ticket=111111, symbol="XAUUSD", side=Side.BUY, lots=Decimal("0.01"), open_price=Decimal("2000")),
            ],
            account_margin=Decimal("4.34"),
        )
    )
    await engine.step()
    add_order = engine.active_order
    assert add_order is not None
    await client.simulate_fill(add_order.order_id, Decimal("1"), add_order.price)
    await engine.step()
    add_command = mt4.next_command()
    mt4.submit_report(
        Mt4Report(
            command_id=add_command["command_id"],
            status="ok",
            action="BUY",
            ticket=222222,
            fill_price=Decimal("2000"),
            lots=Decimal("0.01"),
        )
    )
    await engine.step()

    client.set_quote(Decimal("2000.0"), Decimal("2000.1"))
    mt4.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            bid=Decimal("2000.0"),
            ask=Decimal("2000.1"),
            positions=[
                Mt4Position(ticket=111111, symbol="XAUUSD", side=Side.BUY, lots=Decimal("0.01"), open_price=Decimal("2000")),
                Mt4Position(ticket=222222, symbol="XAUUSD", side=Side.BUY, lots=Decimal("0.01"), open_price=Decimal("2000")),
            ],
            account_margin=Decimal("8.68"),
        )
    )
    await engine.step()
    exit_order = engine.active_order
    assert exit_order is not None
    assert exit_order.reduce_only is True
    assert exit_order.orig_qty == Decimal("2")
    await client.simulate_fill(exit_order.order_id, Decimal("2"), exit_order.price)
    await engine.step()

    close_one = mt4.next_command()
    close_two = mt4.next_command()
    assert close_one["action"] == "CLOSE"
    assert close_two["action"] == "CLOSE"
    assert {close_one["ticket"], close_two["ticket"]} == {111111, 222222}
    assert Decimal(str(close_one["lots"])) == Decimal("0.01")
    assert Decimal(str(close_two["lots"])) == Decimal("0.01")

    mt4.submit_report(
        Mt4Report(
            command_id=close_one["command_id"],
            status="ok",
            action="CLOSE",
            ticket=close_one["ticket"],
            fill_price=Decimal("2000"),
            lots=Decimal("0.01"),
        )
    )
    await engine.step()
    assert engine.state == StrategyState.CLOSING_MT4

    mt4.submit_report(
        Mt4Report(
            command_id=close_two["command_id"],
            status="ok",
            action="CLOSE",
            ticket=close_two["ticket"],
            fill_price=Decimal("2000"),
            lots=Decimal("0.01"),
        )
    )
    await engine.step()
    assert engine.state == StrategyState.IDLE
    assert engine.open_pair is None


@pytest.mark.asyncio
async def test_add_position_uses_dollar_growth_not_relative_growth(tmp_path):
    engine, client, mt4 = await make_engine(
        tmp_path,
        PositionTrackingPaperClient,
        settings_kwargs={"ADD_EDGE_GROWTH_USD": Decimal("1"), "MAX_ADD_COUNT": 2},
    )
    await open_live_pair(engine, client, mt4, ticket=111111)

    client.set_quote(Decimal("2001.01"), Decimal("2002.02"))
    mt4.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            bid=Decimal("1999"),
            ask=Decimal("2000"),
            positions=[
                Mt4Position(ticket=111111, symbol="XAUUSD", side=Side.BUY, lots=Decimal("0.01"), open_price=Decimal("2000")),
            ],
            account_margin=Decimal("4.34"),
        )
    )
    await engine.step()

    assert engine.state == StrategyState.PAIR_OPEN
    assert engine.active_order is None


@pytest.mark.asyncio
async def test_add_position_trigger_uses_actual_initial_entry_spread(tmp_path):
    engine, client, mt4 = await make_engine(
        tmp_path,
        PositionTrackingPaperClient,
        settings_kwargs={"ADD_EDGE_GROWTH_USD": Decimal("1"), "MAX_ADD_COUNT": 2},
    )
    await open_live_pair(engine, client, mt4, ticket=111111, mt4_fill_price=Decimal("2000.5"))
    assert engine.open_pair is not None
    assert engine.open_pair.base_edge == Decimal("1.5")

    client.set_quote(Decimal("2001.6"), Decimal("2002.6"))
    mt4.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            bid=Decimal("1999"),
            ask=Decimal("2000"),
            positions=[
                Mt4Position(ticket=111111, symbol="XAUUSD", side=Side.BUY, lots=Decimal("0.01"), open_price=Decimal("2000.5")),
            ],
            account_margin=Decimal("4.34"),
        )
    )
    await engine.step()

    assert engine.active_order is not None
    assert engine.adding_to_pair is True


@pytest.mark.asyncio
async def test_add_position_trigger_recalculates_legacy_pair_actual_spread(tmp_path):
    engine, client, mt4 = await make_engine(
        tmp_path,
        PositionTrackingPaperClient,
        settings_kwargs={"ADD_EDGE_GROWTH_USD": Decimal("1"), "MAX_ADD_COUNT": 2},
    )
    engine.open_pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("2002"),
        mt4_entry_price=Decimal("2000"),
        binance_order_id="legacy",
        mt4_ticket=111111,
        mt4_tickets=[111111],
        base_edge=Decimal("2"),
        last_add_edge=Decimal("2"),
        add_count=0,
    )
    engine.state = StrategyState.PAIR_OPEN
    client._orders.clear()
    client._orders["legacy"] = OrderUpdate(
        order_id="legacy",
        client_order_id="legacy",
        symbol="XAUUSDT",
        side=Side.SELL,
        status=OrderStatus.FILLED,
        price=Decimal("2002"),
        orig_qty=Decimal("1"),
        executed_qty=Decimal("1"),
        avg_price=Decimal("2002"),
    )
    client.set_quote(Decimal("2001.6"), Decimal("2002.6"))
    mt4.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            bid=Decimal("1999"),
            ask=Decimal("2000"),
            positions=[
                Mt4Position(ticket=111111, symbol="XAUUSD", side=Side.BUY, lots=Decimal("0.01"), open_price=Decimal("2000.5")),
            ],
            account_margin=Decimal("4.34"),
        )
    )

    await engine.step()

    assert engine.active_order is not None
    assert engine.adding_to_pair is True


@pytest.mark.asyncio
async def test_add_position_records_actual_add_fill_spread(tmp_path):
    engine, client, mt4 = await make_engine(
        tmp_path,
        PositionTrackingPaperClient,
        settings_kwargs={"ADD_EDGE_GROWTH_USD": Decimal("1"), "MAX_ADD_COUNT": 2},
    )
    await open_live_pair(engine, client, mt4, ticket=111111)
    client.set_quote(Decimal("2002"), Decimal("2003"))
    mt4.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            bid=Decimal("1999"),
            ask=Decimal("2000"),
            positions=[
                Mt4Position(ticket=111111, symbol="XAUUSD", side=Side.BUY, lots=Decimal("0.01"), open_price=Decimal("2000")),
            ],
            account_margin=Decimal("4.34"),
        )
    )
    await engine.step()

    add_order = engine.active_order
    assert add_order is not None
    assert engine.active_plan is not None
    assert engine.active_plan.edge == Decimal("3")
    await client.simulate_fill(add_order.order_id, Decimal("1"), Decimal("2004.2"))
    await engine.step()
    add_command = mt4.next_command()
    mt4.submit_report(
        Mt4Report(
            command_id=add_command["command_id"],
            status="ok",
            action="BUY",
            ticket=222222,
            fill_price=Decimal("2000.2"),
            lots=Decimal("0.01"),
        )
    )
    await engine.step()

    assert engine.open_pair is not None
    assert engine.open_pair.last_add_edge == Decimal("4.0")


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


@pytest.mark.asyncio
async def test_close_trigger_uses_actual_entry_spread_without_close_max_cap(tmp_path):
    engine, client, mt4 = await make_engine(
        tmp_path,
        settings_kwargs={"CLOSE_MAX_SPREAD": Decimal("1.0"), "CLOSE_PROFIT_USD_PER_OZ": Decimal("0.8"), "MT4_SLIPPAGE_POINTS": 0},
    )
    client.maker_fee_rate = Decimal("0")
    engine.open_pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("4178.59"),
        mt4_entry_price=Decimal("4176.67"),
        binance_order_id="entry-1",
        mt4_ticket=76804334,
        mt4_tickets=[76804334],
    )
    mt4.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            bid=Decimal("4175"),
            ask=Decimal("4175.3"),
            positions=[
                Mt4Position(
                    ticket=76804334,
                    symbol="XAUUSD",
                    side=Side.BUY,
                    lots=Decimal("0.01"),
                    open_price=Decimal("4176.67"),
                )
            ],
        )
    )

    trigger = await engine._close_trigger_spread()

    assert trigger == Decimal("1.12")


@pytest.mark.asyncio
async def test_close_trigger_reserves_mt4_follow_slippage_buffer(tmp_path):
    engine, client, mt4 = await make_engine(
        tmp_path,
        settings_kwargs={"CLOSE_PROFIT_USD_PER_OZ": Decimal("0.3"), "MT4_SLIPPAGE_POINTS": 50},
    )
    client.maker_fee_rate = Decimal("0")
    engine.open_pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("4178.59"),
        mt4_entry_price=Decimal("4176.67"),
        binance_order_id="entry-1",
        mt4_ticket=76804334,
        mt4_tickets=[76804334],
    )
    mt4.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            bid=Decimal("4175"),
            ask=Decimal("4175.3"),
            point=Decimal("0.01"),
            positions=[
                Mt4Position(
                    ticket=76804334,
                    symbol="XAUUSD",
                    side=Side.BUY,
                    lots=Decimal("0.01"),
                    open_price=Decimal("4176.67"),
                )
            ],
        )
    )

    trigger = await engine._close_trigger_spread()

    assert trigger == Decimal("1.12")


@pytest.mark.asyncio
async def test_aged_pair_reduces_close_profit_target_but_keeps_follow_buffer(tmp_path):
    engine, client, mt4 = await make_engine(
        tmp_path,
        settings_kwargs={
            "CLOSE_PROFIT_USD_PER_OZ": Decimal("0.8"),
            "AGED_CLOSE_PROFIT_USD_PER_OZ": Decimal("0.1"),
            "MAX_PAIR_AGE_MINUTES": 60,
            "MT4_SLIPPAGE_POINTS": 30,
        },
    )
    client.maker_fee_rate = Decimal("0")
    engine.open_pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("4178.59"),
        mt4_entry_price=Decimal("4176.67"),
        binance_order_id="entry-1",
        mt4_ticket=76804334,
        mt4_tickets=[76804334],
        opened_ms=utc_now_ms() - 60 * 60_000,
    )
    mt4.update_tick(
        Mt4Tick(
            symbol="XAUUSD",
            bid=Decimal("4175"),
            ask=Decimal("4175.3"),
            point=Decimal("0.01"),
            positions=[
                Mt4Position(
                    ticket=76804334,
                    symbol="XAUUSD",
                    side=Side.BUY,
                    lots=Decimal("0.01"),
                    open_price=Decimal("4176.67"),
                )
            ],
        )
    )

    trigger = await engine._close_trigger_spread()

    assert trigger == Decimal("1.52")


class FillOnCancelPaperClient(PaperBinanceClient):
    async def cancel_order(self, order_id: str):
        order = self._orders.get(order_id)
        if not order:
            return None
        if order.status in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}:
            order = order.model_copy(
                update={
                    "status": OrderStatus.FILLED,
                    "executed_qty": order.orig_qty,
                    "avg_price": order.price,
                }
            )
            self._orders[order_id] = order
        return order


class NotVisibleOncePaperClient(PaperBinanceClient):
    def __init__(self, settings):
        super().__init__(settings)
        self.missing_once = True

    async def get_order(self, order_id: str):
        if self.missing_once:
            self.missing_once = False
            raise BinanceError('{"code":-2013,"msg":"Order does not exist."}')
        return await super().get_order(order_id)


class PositionTrackingPaperClient(PaperBinanceClient):
    async def position_quantity(self) -> Decimal:
        qty = Decimal("0")
        for order in self._orders.values():
            if order.status not in {OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}:
                continue
            signed = order.executed_qty if order.side == Side.BUY else -order.executed_qty
            qty += signed
        return qty


async def open_live_pair(
    engine,
    client,
    mt4,
    ticket: int = 123456,
    binance_fill_price: Decimal = Decimal("2002"),
    mt4_fill_price: Decimal = Decimal("2000"),
):
    await engine.step()
    entry_order = engine.active_order
    assert entry_order is not None
    await client.simulate_fill(entry_order.order_id, Decimal("1"), binance_fill_price)
    await engine.step()
    entry_command = mt4.next_command()
    mt4.submit_report(
        Mt4Report(
            command_id=entry_command["command_id"],
            status="ok",
            action="BUY",
            ticket=ticket,
            fill_price=mt4_fill_price,
            lots=Decimal("0.01"),
        )
    )
    await engine.step()
    assert engine.state == StrategyState.PAIR_OPEN


async def make_engine(tmp_path, client_cls=PaperBinanceClient, settings_kwargs=None):
    cfg = settings(
        tmp_path,
        PAPER_MODE=False,
        LIVE_TRADING=True,
        BINANCE_API_KEY="test-key",
        BINANCE_API_SECRET="test-secret",
        **(settings_kwargs or {}),
    )
    client = client_cls(cfg)
    client.set_quote(Decimal("2001"), Decimal("2002"))
    mt4 = Mt4Bridge(cfg)
    mt4.update_tick(Mt4Tick(symbol="XAUUSD", bid=Decimal("1999"), ask=Decimal("2000")))
    store = Storage(cfg.sqlite_path)
    engine = StrategyEngine(cfg, client, mt4, RiskManager(cfg, store), store)
    await client.start()
    return engine, client, mt4
