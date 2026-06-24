from decimal import Decimal

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
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4007"), ask=Decimal("4007.5")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("4000"), ask=Decimal("4000.2")),
        binance_bars=binance_bars,
        open_pair=None,
        metrics=PositionMetrics(),
    )

    assert status["short_entry"]["threshold"] == Decimal("7.30")
    assert status["short_entry"]["ready"] is True
    assert status["selected_entry"]["direction"] == PairDirection.BINANCE_SHORT_MT4_LONG.value


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
        actual_entry_spread=Decimal("1.8"),
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
