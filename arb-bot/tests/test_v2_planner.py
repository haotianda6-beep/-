from decimal import Decimal
from datetime import datetime, timezone

from app.config import Settings
from app.models import ExchangeFilters, HistoryBar, MarketQuote, OpenPair, PairDirection, PositionMetrics, utc_now_ms
from app.storage import Storage
from app.v2_planner import build_gold_v2_status


def settings(tmp_path, **kwargs) -> Settings:
    defaults = {
        "PAPER_MODE": True,
        "LIVE_TRADING": False,
        "SQLITE_PATH": tmp_path / "test.sqlite3",
        "OPEN_MIN_EDGE": Decimal("1.50"),
        "BINANCE_ENTRY_OFFSET_USD": Decimal("0.10"),
        "TARGET_OZ": Decimal("1"),
        "MT4_SLIPPAGE_POINTS": 30,
        "MT4_CLOSE_EXTRA_BUFFER_USD": Decimal("0"),
    }
    defaults.update(kwargs)
    return Settings(_env_file=None, **defaults)


def filters() -> ExchangeFilters:
    return ExchangeFilters(tick_size=Decimal("0.1"), qty_step=Decimal("0.001"), min_qty=Decimal("0.001"))


def bar(open_time_ms: int, close: Decimal) -> HistoryBar:
    return HistoryBar(open_time_ms=open_time_ms, open=close, high=close, low=close, close=close)


def recent_bars(diffs: list[Decimal]) -> tuple[list[HistoryBar], list[HistoryBar]]:
    start = utc_now_ms() - len(diffs) * 60_000
    mt4 = []
    binance = []
    for index, diff in enumerate(diffs):
        open_time = start + index * 60_000
        mt4.append(bar(open_time, Decimal("4000")))
        binance.append(bar(open_time, Decimal("4000") + diff))
    return mt4, binance


def test_v2_uses_upper_range_threshold_from_recent_spreads(tmp_path):
    cfg = settings(tmp_path)
    store = Storage(cfg.sqlite_path)
    mt4_bars, binance_bars = recent_bars([Decimal(i) for i in range(1, 11)])
    store.upsert_bars("mt4", cfg.mt4_symbol, "1m", mt4_bars)

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4007.8"), ask=Decimal("4008.0")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("4000"), ask=Decimal("4000.2")),
        binance_bars=binance_bars,
        open_pair=None,
        metrics=PositionMetrics(),
    )

    assert status["short_entry"]["threshold"] == Decimal("7.30")
    assert status["short_entry"]["ready"] is True
    assert status["selected_entry"]["direction"] == PairDirection.BINANCE_SHORT_MT4_LONG.value


def test_v2_blocks_entry_when_recent_range_has_no_safe_exit(tmp_path):
    cfg = settings(tmp_path, CLOSE_PROFIT_USD_PER_OZ=Decimal("2.5"))
    store = Storage(cfg.sqlite_path)
    mt4_bars, binance_bars = recent_bars([Decimal("3.0"), Decimal("3.2"), Decimal("3.4"), Decimal("3.6"), Decimal("3.8"), Decimal("4.0")] * 2)
    store.upsert_bars("mt4", cfg.mt4_symbol, "1m", mt4_bars)

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4005.0"), ask=Decimal("4005.2")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("3999.8"), ask=Decimal("4000.0")),
        binance_bars=binance_bars,
        open_pair=None,
        metrics=PositionMetrics(),
    )

    assert status["short_entry"]["current_edge"] >= status["short_entry"]["threshold"]
    assert status["short_entry"]["exit_viable"] is False
    assert status["short_entry"]["ready"] is False
    assert "安全平仓" in status["short_entry"]["reason"]


def test_v2_blocks_entry_until_current_edge_covers_entry_slippage_budget(tmp_path):
    cfg = settings(tmp_path, OPEN_MIN_EDGE=Decimal("2.0"), MT4_SLIPPAGE_POINTS=0, MT4_CLOSE_EXTRA_BUFFER_USD=Decimal("5.0"))
    store = Storage(cfg.sqlite_path)
    mt4_bars, binance_bars = recent_bars([Decimal("2.0")] * 10)
    store.upsert_bars("mt4", cfg.mt4_symbol, "1m", mt4_bars)

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4002.0"), ask=Decimal("4002.4")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("4000.0"), ask=Decimal("4000.2")),
        binance_bars=binance_bars,
        open_pair=None,
        metrics=PositionMetrics(),
    )

    assert status["short_entry"]["current_edge"] == Decimal("2.2")
    assert status["short_entry"]["threshold"] == Decimal("2.0")
    assert status["mt4_slippage_budget"] == Decimal("0.3")
    assert status["short_entry"]["required_edge"] == Decimal("2.3")
    assert status["short_entry"]["ready"] is False
    assert "安全入场边际" in status["short_entry"]["reason"]


def test_v2_blocks_entry_when_quote_gap_is_unreasonable(tmp_path):
    cfg = settings(tmp_path)
    store = Storage(cfg.sqlite_path)
    mt4_bars, binance_bars = recent_bars([Decimal("2")] * 10)
    store.upsert_bars("mt4", cfg.mt4_symbol, "1m", mt4_bars)

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4000"), ask=Decimal("4000.2")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("3000"), ask=Decimal("3000.2")),
        binance_bars=binance_bars,
        open_pair=None,
        metrics=PositionMetrics(),
    )

    assert status["short_entry"]["ready"] is False
    assert status["short_entry"]["current_edge"] is None
    assert "报价异常" in status["short_entry"]["reason"]
    assert status["selected_entry"]["reason"].startswith("报价异常")


def test_v2_ignores_unreasonable_historical_bar_gap(tmp_path):
    cfg = settings(tmp_path)
    store = Storage(cfg.sqlite_path)
    mt4_bars, binance_bars = recent_bars([Decimal("2"), Decimal("3"), Decimal("999"), Decimal("4"), Decimal("5")] * 2)
    store.upsert_bars("mt4", cfg.mt4_symbol, "1m", mt4_bars)

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4005"), ask=Decimal("4005.2")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("4000"), ask=Decimal("4000.2")),
        binance_bars=binance_bars,
        open_pair=None,
        metrics=PositionMetrics(),
    )

    assert status["short_range"]["discarded"] == 2
    assert status["short_range"]["high"] == Decimal("5")
    assert status["short_entry"]["threshold"] == Decimal("4.1")


def test_v2_blocks_entry_when_next_triple_swap_makes_exit_unsafe(tmp_path):
    cfg = settings(
        tmp_path,
        CLOSE_PROFIT_USD_PER_OZ=Decimal("0.20"),
        MT4_TRIPLE_SWAP_WEEKDAY=2,
        MT4_TRIPLE_SWAP_MULTIPLIER=Decimal("3"),
    )
    store = Storage(cfg.sqlite_path)
    mt4_bars, binance_bars = recent_bars([Decimal("2.4"), Decimal("2.6"), Decimal("2.8"), Decimal("3.0")] * 3)
    store.upsert_bars("mt4", cfg.mt4_symbol, "1m", mt4_bars)

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4003.4"), ask=Decimal("4003.6")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("3999.8"), ask=Decimal("4000.0")),
        binance_bars=binance_bars,
        open_pair=None,
        metrics=PositionMetrics(
            binance_funding_rate=Decimal("0"),
            mt4_swap_long_per_lot=Decimal("-65.96"),
            mt4_swap_short_per_lot=Decimal("27.09"),
            mt4_swap_type=0,
            mt4_next_rollover_time_ms=int(datetime(2026, 7, 1, 20, 59, tzinfo=timezone.utc).timestamp() * 1000),
        ),
    )

    assert status["short_entry"]["current_edge"] >= status["short_entry"]["threshold"]
    assert status["short_entry"]["next_settlement_adjustment"]["mt4_swap"] == Decimal("-1.9788")
    assert status["short_entry"]["estimated_exit_target_spread"] == Decimal("0.7212")
    assert status["short_entry"]["exit_viable"] is False
    assert status["short_entry"]["ready"] is False
    assert "隔夜费" in status["short_entry"]["reason"]


def test_v2_blocks_entry_when_exit_buffer_exceeds_locked_edge(tmp_path):
    cfg = settings(tmp_path, OPEN_MIN_EDGE=Decimal("2.0"), MT4_CLOSE_EXTRA_BUFFER_USD=Decimal("5.0"))
    store = Storage(cfg.sqlite_path)
    mt4_bars, binance_bars = recent_bars([Decimal("2.0"), Decimal("2.2"), Decimal("2.4")] * 4)
    store.upsert_bars("mt4", cfg.mt4_symbol, "1m", mt4_bars)

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4003.8"), ask=Decimal("4004.0")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("4000.8"), ask=Decimal("4001.0")),
        binance_bars=binance_bars,
        open_pair=None,
        metrics=PositionMetrics(),
        mt4_tick_move_budget=Decimal("0.1"),
    )

    assert status["short_entry"]["current_edge"] >= status["short_entry"]["threshold"]
    assert status["short_entry"]["estimated_exit_target_spread"] == Decimal("0")
    assert status["short_entry"]["exit_viable"] is False
    assert status["short_entry"]["ready"] is False
    assert "安全平仓" in status["short_entry"]["reason"]


def test_v2_blocks_entry_when_mt4_rollover_time_is_stale(tmp_path):
    cfg = settings(tmp_path)
    store = Storage(cfg.sqlite_path)
    mt4_bars, binance_bars = recent_bars([Decimal("2")] * 10)
    store.upsert_bars("mt4", cfg.mt4_symbol, "1m", mt4_bars)

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4005.0"), ask=Decimal("4005.2")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("3999.8"), ask=Decimal("4000.0")),
        binance_bars=binance_bars,
        open_pair=None,
        metrics=PositionMetrics(mt4_next_rollover_time_ms=1),
    )

    assert status["short_entry"]["ready"] is False
    assert "结算时间已过期" in status["short_entry"]["reason"]
    assert status["selected_entry"]["reason"] == status["short_entry"]["reason"]


def test_v2_short_order_price_keeps_threshold_and_slippage_budget(tmp_path):
    cfg = settings(tmp_path)
    store = Storage(cfg.sqlite_path)
    mt4_bars, binance_bars = recent_bars([Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")] * 3)
    store.upsert_bars("mt4", cfg.mt4_symbol, "1m", mt4_bars)

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4003.0"), ask=Decimal("4003.2")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("3999.8"), ask=Decimal("4000.0")),
        binance_bars=binance_bars,
        open_pair=None,
        metrics=PositionMetrics(),
    )

    assert status["short_entry"]["threshold"] == Decimal("3.10")
    assert status["mt4_slippage_budget"] == Decimal("0.5")
    assert status["mt4_live_spread_usd_per_oz"] == Decimal("0.2")
    assert status["short_entry"]["binance_price"] == Decimal("4003.6")
    assert status["short_entry"]["expected_locked_edge"] == Decimal("3.6")


def test_v2_slippage_budget_includes_recent_mt4_movement(tmp_path):
    cfg = settings(tmp_path, MT4_SLIPPAGE_POINTS=0)
    store = Storage(cfg.sqlite_path)
    start = utc_now_ms() - 10 * 60_000
    closes = [
        Decimal("4000.0"),
        Decimal("4000.2"),
        Decimal("4001.2"),
        Decimal("4001.5"),
        Decimal("4003.5"),
        Decimal("4004.0"),
        Decimal("4004.8"),
        Decimal("4006.3"),
        Decimal("4006.5"),
        Decimal("4007.7"),
    ]
    mt4_bars = [bar(start + index * 60_000, close) for index, close in enumerate(closes)]
    binance_bars = [bar(item.open_time_ms, item.close + Decimal("2.0")) for item in mt4_bars]
    store.upsert_bars("mt4", cfg.mt4_symbol, "1m", mt4_bars)

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4010.0"), ask=Decimal("4010.2")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("4007.0"), ask=Decimal("4007.3")),
        binance_bars=binance_bars,
        open_pair=None,
        metrics=PositionMetrics(),
    )

    assert status["short_entry"]["threshold"] == Decimal("2.00")
    assert status["mt4_slippage_budget"] == Decimal("1.3")
    assert status["short_entry"]["binance_price"] == Decimal("4010.6")
    assert status["short_entry"]["expected_locked_edge"] == Decimal("3.3")


def test_v2_slippage_budget_prefers_realtime_tick_movement(tmp_path):
    cfg = settings(tmp_path, MT4_SLIPPAGE_POINTS=0)
    store = Storage(cfg.sqlite_path)
    start = utc_now_ms() - 10 * 60_000
    mt4_bars = [bar(start + index * 60_000, Decimal("4000") + Decimal(index)) for index in range(10)]
    binance_bars = [bar(item.open_time_ms, item.close + Decimal("2.0")) for item in mt4_bars]
    store.upsert_bars("mt4", cfg.mt4_symbol, "1m", mt4_bars)

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4010.0"), ask=Decimal("4010.2")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("4007.0"), ask=Decimal("4007.2")),
        binance_bars=binance_bars,
        open_pair=None,
        metrics=PositionMetrics(),
        mt4_tick_move_budget=Decimal("0.1"),
    )

    assert status["mt4_slippage_budget"] == Decimal("0.4")
    assert status["mt4_move_budget_source"] == "实时tick"


def test_v2_entry_slippage_budget_excludes_mt4_close_extra_buffer(tmp_path):
    cfg = settings(tmp_path, MT4_CLOSE_EXTRA_BUFFER_USD=Decimal("0.8"))
    store = Storage(cfg.sqlite_path)
    mt4_bars, binance_bars = recent_bars([Decimal("2")] * 10)
    store.upsert_bars("mt4", cfg.mt4_symbol, "1m", mt4_bars)

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4003"), ask=Decimal("4003.2")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("3999.8"), ask=Decimal("4000.0")),
        binance_bars=binance_bars,
        open_pair=None,
        metrics=PositionMetrics(),
    )

    assert status["mt4_slippage_budget"] == Decimal("0.5")
    assert status["short_entry"]["binance_price"] == Decimal("4003.3")
    assert status["short_entry"]["expected_locked_edge"] == Decimal("3.3")


def test_v2_add_plan_uses_real_first_edge_plus_step(tmp_path):
    cfg = settings(tmp_path, ADD_EDGE_GROWTH_USD=Decimal("1"))
    store = Storage(cfg.sqlite_path)
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("4002"),
        mt4_entry_price=Decimal("4000.2"),
        binance_order_id="entry",
        add_count=2,
    )

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4005"), ask=Decimal("4005.2")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("4001.8"), ask=Decimal("4002.0")),
        binance_bars=[],
        open_pair=pair,
        metrics=PositionMetrics(actual_entry_spread=Decimal("1.8")),
    )

    assert status["add_plan"]["enabled"] is True
    assert status["add_plan"]["next_add_number"] == 3
    assert status["add_plan"]["next_trigger_edge"] == Decimal("4.8")
    assert status["add_plan"]["ready"] is False


def test_v2_add_plan_does_not_lower_current_average_edge(tmp_path):
    cfg = settings(tmp_path, ADD_EDGE_GROWTH_USD=Decimal("1"), MT4_SLIPPAGE_POINTS=37)
    store = Storage(cfg.sqlite_path)
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("2"),
        binance_entry_price=Decimal("4030.76"),
        mt4_entry_price=Decimal("4022.49"),
        binance_order_id="entry/add",
        base_edge=Decimal("2.42"),
        add_count=1,
    )

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4021.0"), ask=Decimal("4021.2")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("4016.4"), ask=Decimal("4016.7")),
        binance_bars=[],
        open_pair=pair,
        metrics=PositionMetrics(actual_entry_spread=Decimal("8.27")),
        mt4_tick_move_budget=Decimal("0.37"),
    )

    add_plan = status["add_plan"]
    assert add_plan["next_trigger_edge"] == Decimal("4.42")
    assert add_plan["average_protection_edge"] == Decimal("8.27")
    assert add_plan["next_actionable_trigger_edge"] == Decimal("8.27")
    assert add_plan["current_edge"] >= add_plan["next_trigger_edge"]
    assert add_plan["current_edge"] < add_plan["next_actionable_trigger_edge"]
    assert add_plan["estimated_blended_edge"] >= Decimal("8.27")
    assert add_plan["ready"] is False


def test_v2_add_plan_blocks_when_blended_edge_cannot_cover_exit_buffer(tmp_path):
    cfg = settings(tmp_path, ADD_EDGE_GROWTH_USD=Decimal("1"), MT4_CLOSE_EXTRA_BUFFER_USD=Decimal("5.0"))
    store = Storage(cfg.sqlite_path)
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("4013.38"),
        mt4_entry_price=Decimal("4010.96"),
        binance_order_id="entry",
        base_edge=Decimal("2.42"),
    )

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4020.5"), ask=Decimal("4020.7")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("4017.0"), ask=Decimal("4017.2")),
        binance_bars=[],
        open_pair=pair,
        metrics=PositionMetrics(actual_entry_spread=Decimal("2.42")),
        mt4_tick_move_budget=Decimal("0.1"),
    )

    assert status["add_plan"]["current_edge"] >= status["add_plan"]["next_trigger_edge"]
    assert status["add_plan"]["current_edge"] < status["add_plan"]["next_actionable_trigger_edge"]
    assert status["add_plan"]["required_locked_edge"] > status["add_plan"]["next_trigger_edge"]
    assert status["add_plan"]["exit_viable"] is True
    assert status["add_plan"]["ready"] is False
    assert "安全触发位" in status["add_plan"]["reason"]


def test_v2_add_plan_accepts_exact_required_blended_edge(tmp_path):
    cfg = settings(
        tmp_path,
        ADD_EDGE_GROWTH_USD=Decimal("1"),
        CLOSE_PROFIT_USD_PER_OZ=Decimal("0.30"),
        MT4_CLOSE_EXTRA_BUFFER_USD=Decimal("5.0"),
        MT4_SLIPPAGE_POINTS=0,
    )
    store = Storage(cfg.sqlite_path)
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("4013.38"),
        mt4_entry_price=Decimal("4010.96"),
        binance_order_id="entry",
        base_edge=Decimal("2.42"),
    )

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=ExchangeFilters(tick_size=Decimal("0.01"), qty_step=Decimal("0.001"), min_qty=Decimal("0.001")),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4020.50"), ask=Decimal("4020.70")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("4017.00"), ask=Decimal("4017.20")),
        binance_bars=[],
        open_pair=pair,
        metrics=PositionMetrics(actual_entry_spread=Decimal("2.42"), close_profit_usd_per_oz=Decimal("0.30")),
        mt4_tick_move_budget=Decimal("0.38"),
    )

    assert status["add_plan"]["expected_locked_edge"] == Decimal("8.18")
    assert status["add_plan"]["estimated_blended_edge"] == status["add_plan"]["required_blended_edge"]
    assert status["add_plan"]["exit_viable"] is True


def test_v2_negative_swap_window_relaxes_exit_to_safe_target(tmp_path):
    cfg = settings(
        tmp_path,
        CLOSE_MAX_SPREAD=Decimal("1.50"),
        CLOSE_PROFIT_USD_PER_OZ=Decimal("0.60"),
        NEGATIVE_SWAP_CLOSE_BEFORE_MINUTES=30,
    )
    store = Storage(cfg.sqlite_path)
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("4002"),
        mt4_entry_price=Decimal("4000.2"),
        binance_order_id="entry",
    )
    metrics = PositionMetrics(
        actual_entry_spread=Decimal("1.0"),
        current_exit_spread=Decimal("1.55"),
        profitable_spread_threshold=Decimal("1.8"),
        dynamic_close_spread=Decimal("1.0"),
        exit_follow_buffer_usd_per_oz=Decimal("0.2"),
        mt4_swap_estimate=Decimal("-1.0"),
        mt4_next_rollover_time_ms=utc_now_ms() + 5 * 60_000,
        estimated_fees=Decimal("0"),
        binance_accrued_funding=Decimal("0"),
        mt4_accrued_swap=Decimal("0"),
    )

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4001"), ask=Decimal("4001.1")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("3999.5"), ask=Decimal("3999.8")),
        binance_bars=[],
        open_pair=pair,
        metrics=metrics,
    )

    exit_plan = status["exit_plan"]
    assert exit_plan["negative_swap"]["active"] is True
    assert exit_plan["normal_target_exit_spread"] == Decimal("1.0")
    assert exit_plan["target_exit_spread"] == Decimal("1.6")


def test_v2_loss_limit_relaxes_exit_to_max_loss_target(tmp_path):
    cfg = settings(
        tmp_path,
        MAX_PAIR_LOSS_USDT=Decimal("1.5"),
        CLOSE_PROFIT_USD_PER_OZ=Decimal("0.60"),
    )
    store = Storage(cfg.sqlite_path)
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("4002"),
        mt4_entry_price=Decimal("4000.2"),
        binance_order_id="entry",
    )
    metrics = PositionMetrics(
        actual_entry_spread=Decimal("1.8"),
        current_exit_spread=Decimal("3.2"),
        profitable_spread_threshold=Decimal("1.8"),
        dynamic_close_spread=Decimal("1.0"),
        estimated_close_net=Decimal("-1.6"),
    )

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4003.2"), ask=Decimal("4003.3")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("4000.0"), ask=Decimal("4000.3")),
        binance_bars=[],
        open_pair=pair,
        metrics=metrics,
    )

    exit_plan = status["exit_plan"]
    assert exit_plan["loss_limit"]["active"] is True
    assert exit_plan["normal_target_exit_spread"] == Decimal("1.0")
    assert exit_plan["target_exit_spread"] == Decimal("3.3")
    assert "最大亏损" in exit_plan["reason"]


def test_v2_negative_swap_safe_target_does_not_go_negative(tmp_path):
    cfg = settings(
        tmp_path,
        CLOSE_PROFIT_USD_PER_OZ=Decimal("0.60"),
        NEGATIVE_SWAP_CLOSE_BEFORE_MINUTES=30,
    )
    store = Storage(cfg.sqlite_path)
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("4002"),
        mt4_entry_price=Decimal("4000.2"),
        binance_order_id="entry",
    )
    metrics = PositionMetrics(
        actual_entry_spread=Decimal("1.0"),
        current_exit_spread=Decimal("1.55"),
        profitable_spread_threshold=Decimal("1.0"),
        dynamic_close_spread=Decimal("0"),
        exit_follow_buffer_usd_per_oz=Decimal("2.0"),
        mt4_swap_estimate=Decimal("-1.0"),
        mt4_next_rollover_time_ms=utc_now_ms() + 5 * 60_000,
    )

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4001"), ask=Decimal("4001.1")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("3999.5"), ask=Decimal("3999.8")),
        binance_bars=[],
        open_pair=pair,
        metrics=metrics,
    )

    exit_plan = status["exit_plan"]
    assert exit_plan["negative_swap"]["active"] is True
    assert exit_plan["target_exit_spread"] == Decimal("0")
