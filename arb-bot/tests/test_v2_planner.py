from decimal import Decimal
from datetime import datetime, timezone

import pytest

import app.v2_planner as v2_planner
from app.config import Settings
from app.market_calendar import is_xau_weekend_ms as real_is_xau_weekend_ms
from app.models import ExchangeFilters, HistoryBar, MarketQuote, OpenPair, PairDirection, PositionMetrics, utc_now_ms
from app.storage import Storage
from app.v2_planner import (
    _entry_threshold,
    _objective_health,
    _spread_values,
    _threshold_with_performance_penalty,
    build_gold_v2_status,
)


@pytest.fixture(autouse=True)
def open_market_calendar(monkeypatch):
    monkeypatch.setattr(v2_planner, "is_xau_weekend_ms", lambda timestamp_ms: False)


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
        "GOLD_V2_MIN_ENTRY_INTERVAL_MS": 0,
    }
    defaults.update(kwargs)
    return Settings(_env_file=None, **defaults)


def filters() -> ExchangeFilters:
    return ExchangeFilters(tick_size=Decimal("0.1"), qty_step=Decimal("0.001"), min_qty=Decimal("0.001"))


def bar(open_time_ms: int, close: Decimal) -> HistoryBar:
    return HistoryBar(open_time_ms=open_time_ms, open=close, high=close + Decimal("0.01"), low=close - Decimal("0.01"), close=close)


def ranged_bar(open_time_ms: int, close: Decimal, low: Decimal, high: Decimal) -> HistoryBar:
    return HistoryBar(open_time_ms=open_time_ms, open=close, high=high, low=low, close=close)


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
    mt4_bars, binance_bars = recent_bars([Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4"), Decimal("5"), Decimal("6")] * 3)
    store.upsert_bars("mt4", cfg.mt4_symbol, "1m", mt4_bars)

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4003.5"), ask=Decimal("4003.7")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("4000"), ask=Decimal("4000.2")),
        binance_bars=binance_bars,
        open_pair=None,
        metrics=PositionMetrics(),
    )

    assert status["short_range"]["discarded"] == 6
    assert status["short_entry"]["threshold"] == Decimal("3.10")
    assert status["short_entry"]["ready"] is True
    assert status["selected_entry"]["direction"] == PairDirection.BINANCE_SHORT_MT4_LONG.value


def test_v2_blocks_entry_when_recent_range_has_no_safe_exit(tmp_path):
    cfg = settings(tmp_path, CLOSE_PROFIT_USD_PER_OZ=Decimal("2.5"), MAX_PAIR_AGE_MINUTES=0, MT4_SLIPPAGE_POINTS=0)
    store = Storage(cfg.sqlite_path)
    mt4_bars, binance_bars = recent_bars([Decimal("3.0"), Decimal("3.2"), Decimal("3.4"), Decimal("3.6"), Decimal("3.8"), Decimal("4.0")] * 2)
    store.upsert_bars("mt4", cfg.mt4_symbol, "1m", mt4_bars)

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4003.6"), ask=Decimal("4003.8")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("3999.8"), ask=Decimal("4000.0")),
        binance_bars=binance_bars,
        open_pair=None,
        metrics=PositionMetrics(),
    )

    assert status["short_entry"]["current_edge"] >= status["short_entry"]["threshold"]
    assert status["short_entry"]["exit_viable"] is False
    assert status["short_entry"]["ready"] is False
    assert "安全平仓" in status["short_entry"]["reason"]


def test_v2_entry_feasibility_uses_aged_profit_when_cycle_window_is_short(tmp_path):
    cfg = settings(
        tmp_path,
        OPEN_MIN_EDGE=Decimal("2.4"),
        CLOSE_PROFIT_USD_PER_OZ=Decimal("2.0"),
        AGED_CLOSE_PROFIT_USD_PER_OZ=Decimal("0.3"),
        MAX_PAIR_AGE_MINUTES=15,
        MT4_CLOSE_EXTRA_BUFFER_USD=Decimal("0"),
    )
    store = Storage(cfg.sqlite_path)
    mt4_bars, binance_bars = recent_bars([Decimal("1.5"), Decimal("1.6"), Decimal("1.8"), Decimal("2.0")] * 3)
    store.upsert_bars("mt4", cfg.mt4_symbol, "1m", mt4_bars)

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4002.8"), ask=Decimal("4003.0")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("3999.8"), ask=Decimal("4000.0")),
        binance_bars=binance_bars,
        open_pair=None,
        metrics=PositionMetrics(),
    )

    assert status["short_entry"]["entry_viability_close_profit_usd_per_oz"] == Decimal("0.3")
    assert status["short_entry"]["recent_low_spread"] == Decimal("1.5")
    assert status["short_entry"]["estimated_exit_target_spread"] > Decimal("1.5")
    assert status["short_entry"]["ready"] is True


def test_v2_entry_feasibility_uses_aged_profit_for_short_cycle_model(tmp_path, monkeypatch):
    monkeypatch.setattr(v2_planner, "is_xau_weekend_ms", lambda timestamp_ms: False)
    cfg = settings(
        tmp_path,
        OPEN_MIN_EDGE=Decimal("2.4"),
        CLOSE_PROFIT_USD_PER_OZ=Decimal("2.0"),
        AGED_CLOSE_PROFIT_USD_PER_OZ=Decimal("0.1"),
        MAX_PAIR_AGE_MINUTES=3,
        MT4_SLIPPAGE_POINTS=0,
        MT4_CLOSE_EXTRA_BUFFER_USD=Decimal("0"),
    )
    store = Storage(cfg.sqlite_path)
    mt4_bars, binance_bars = recent_bars([Decimal("3.1"), Decimal("3.2"), Decimal("2.0"), Decimal("1.9")] * 3)
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

    assert status["short_entry"]["entry_viability_close_profit_usd_per_oz"] == Decimal("0.1")
    assert status["entry_model"]["short"]["suggested_threshold"] is not None
    assert status["entry_model"]["short"]["selected"]["aged_close_profit"] == Decimal("0.1")


def test_v2_can_quote_entry_before_visible_edge_covers_slippage_budget(tmp_path):
    cfg = settings(tmp_path, OPEN_MIN_EDGE=Decimal("2.0"), MT4_SLIPPAGE_POINTS=0, MT4_CLOSE_EXTRA_BUFFER_USD=Decimal("0"))
    store = Storage(cfg.sqlite_path)
    mt4_bars, binance_bars = recent_bars([Decimal("1.0"), Decimal("2.0")] * 5)
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
    assert status["short_entry"]["required_edge"] == Decimal("2.0")
    assert status["short_entry"]["locked_edge_floor"] == Decimal("2.3")
    assert status["short_entry"]["expected_locked_edge"] >= Decimal("2.3")
    assert status["short_entry"]["ready"] is True
    assert "挂单触发位" in status["short_entry"]["reason"]


def test_v2_blocks_entry_when_follow_protection_exceeds_normal_gold_gap(tmp_path):
    cfg = settings(tmp_path, OPEN_MIN_EDGE=Decimal("2.0"), MT4_SLIPPAGE_POINTS=220, MT4_CLOSE_EXTRA_BUFFER_USD=Decimal("0"))
    store = Storage(cfg.sqlite_path)
    mt4_bars, binance_bars = recent_bars([Decimal("1.0"), Decimal("2.0")] * 5)
    store.upsert_bars("mt4", cfg.mt4_symbol, "1m", mt4_bars)

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4003.6"), ask=Decimal("4003.8")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("3999.8"), ask=Decimal("4000.0")),
        binance_bars=binance_bars,
        open_pair=None,
        metrics=PositionMetrics(),
    )

    assert status["short_entry"]["locked_edge_floor"] > Decimal("4")
    assert status["short_entry"]["ready"] is False
    assert "成交保护线" in status["short_entry"]["reason"]
    assert "超过黄金正常上限" in status["short_entry"]["reason"]


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


def test_v2_ignores_unreasonable_historical_bar_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(v2_planner, "is_xau_weekend_ms", lambda timestamp_ms: False)
    cfg = settings(tmp_path)
    store = Storage(cfg.sqlite_path)
    mt4_bars, binance_bars = recent_bars([Decimal("2"), Decimal("3"), Decimal("999"), Decimal("4"), Decimal("5")] * 4)
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

    assert status["short_range"]["discarded"] == 8
    assert status["short_range"]["high"] == Decimal("4")
    assert status["short_entry"]["threshold"] <= Decimal("4")


def test_v2_ignores_weekend_training_bars(monkeypatch):
    monkeypatch.setattr(v2_planner, "is_xau_weekend_ms", real_is_xau_weekend_ms)
    saturday = 1_787_985_600_000
    monday = 1_788_158_400_000
    mt4_bars = [
        ranged_bar(saturday, Decimal("4000"), Decimal("3999.9"), Decimal("4000.1")),
        ranged_bar(monday, Decimal("4000"), Decimal("3999.9"), Decimal("4000.1")),
    ]
    binance_bars = [
        ranged_bar(saturday, Decimal("4012.3"), Decimal("4012.2"), Decimal("4012.4")),
        ranged_bar(monday, Decimal("4003"), Decimal("4002.9"), Decimal("4003.1")),
    ]

    short, long, discarded = _spread_values(mt4_bars, binance_bars)

    assert short == [Decimal("3")]
    assert long == [Decimal("-3")]
    assert discarded == 1


def test_v2_ignores_china_calendar_weekend_training_bars(monkeypatch):
    monkeypatch.setattr(v2_planner, "is_xau_weekend_ms", real_is_xau_weekend_ms)
    china_saturday = int(datetime(2026, 6, 26, 17, tzinfo=timezone.utc).timestamp() * 1000)
    monday = int(datetime(2026, 6, 29, 1, tzinfo=timezone.utc).timestamp() * 1000)
    mt4_bars = [
        ranged_bar(china_saturday, Decimal("4000"), Decimal("3999.9"), Decimal("4000.1")),
        ranged_bar(monday, Decimal("4000"), Decimal("3999.9"), Decimal("4000.1")),
    ]
    binance_bars = [
        ranged_bar(china_saturday, Decimal("4012.3"), Decimal("4012.2"), Decimal("4012.4")),
        ranged_bar(monday, Decimal("4003"), Decimal("4002.9"), Decimal("4003.1")),
    ]

    short, long, discarded = _spread_values(mt4_bars, binance_bars)

    assert short == [Decimal("3")]
    assert long == [Decimal("-3")]
    assert discarded == 1


def test_v2_ignores_stale_and_volatile_training_bars():
    base = 1_800_000_000_000
    mt4_bars = [
        ranged_bar(base, Decimal("4000"), Decimal("4000"), Decimal("4000")),
        ranged_bar(base + 60_000, Decimal("4000"), Decimal("4000"), Decimal("4000")),
        ranged_bar(base + 120_000, Decimal("4000"), Decimal("3997"), Decimal("4002")),
        ranged_bar(base + 180_000, Decimal("4000"), Decimal("3999"), Decimal("4001")),
    ]
    binance_bars = [
        ranged_bar(base, Decimal("4003"), Decimal("4002"), Decimal("4004")),
        ranged_bar(base + 60_000, Decimal("4003"), Decimal("4002"), Decimal("4004")),
        ranged_bar(base + 120_000, Decimal("4003"), Decimal("4002"), Decimal("4004")),
        ranged_bar(base + 180_000, Decimal("4003"), Decimal("4002"), Decimal("4004")),
    ]

    short, long, discarded = _spread_values(mt4_bars, binance_bars)

    assert short == [Decimal("3"), Decimal("3")]
    assert long == [Decimal("-3"), Decimal("-3")]
    assert discarded == 2


def test_v2_uses_model_threshold_even_when_above_recent_range():
    threshold = _entry_threshold(
        {"points": 30, "low": Decimal("1.89"), "high": Decimal("3.06"), "latest": Decimal("2.83")},
        Decimal("1.50"),
        {"suggested_threshold": Decimal("3.46")},
    )

    assert threshold == Decimal("3.46")


def test_v2_falls_back_when_model_threshold_exceeds_tradable_ceiling():
    threshold = _entry_threshold(
        {"points": 30, "low": Decimal("1.89"), "high": Decimal("3.42"), "latest": Decimal("2.83")},
        Decimal("1.50"),
        {"suggested_threshold": Decimal("12.37")},
    )

    assert threshold == Decimal("2.9610")


def test_v2_realized_losses_raise_entry_threshold(tmp_path):
    cfg = settings(tmp_path, OPEN_MIN_EDGE=Decimal("2.40"), MT4_SLIPPAGE_POINTS=0)
    store = Storage(cfg.sqlite_path)
    for pnl in ["-1.0", "-0.5", "-2.0"]:
        store.record_event("v2_pair_pnl_recorded", {"realized_pnl": pnl})
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
        metrics=PositionMetrics(),
    )

    assert status["realized_performance"]["sample_count"] == 3
    assert status["realized_performance"]["win_rate"] == Decimal("0")
    assert status["performance_entry_penalty"] == Decimal("0.50")
    assert status["short_entry"]["threshold"] == Decimal("2.820")
    assert status["performance_threshold_cap"]["short"] == Decimal("2.70")
    assert "自动抬高" in status["realized_performance"]["reason"]


def test_v2_realized_performance_override_replaces_event_samples(tmp_path):
    cfg = settings(tmp_path, OPEN_MIN_EDGE=Decimal("2.40"), MT4_SLIPPAGE_POINTS=0)
    store = Storage(cfg.sqlite_path)
    for pnl in ["-1.0", "-0.5", "-2.0"]:
        store.record_event("v2_pair_pnl_recorded", {"realized_pnl": pnl})
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
        metrics=PositionMetrics(),
        realized_performance={
            "sample_count": 4,
            "wins": 4,
            "losses": 0,
            "win_rate": Decimal("1"),
            "target_win_rate": Decimal("0.70"),
            "total_pnl": Decimal("3.2"),
            "average_pnl": Decimal("0.8"),
            "latest_pnl": Decimal("1.0"),
            "min_pnl": Decimal("0.2"),
            "max_pnl": Decimal("1.2"),
            "reason": "真实历史达标",
        },
    )

    assert status["realized_performance"]["wins"] == 4
    assert status["performance_entry_penalty"] == Decimal("0")
    assert status["short_entry"]["threshold"] == Decimal("2.820")


def test_v2_current_guard_performance_replaces_old_losses_for_entry_penalty(tmp_path):
    cfg = settings(tmp_path, OPEN_MIN_EDGE=Decimal("2.40"), MT4_SLIPPAGE_POINTS=0)
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
        metrics=PositionMetrics(),
        realized_performance={
            "sample_count": 14,
            "wins": 4,
            "losses": 10,
            "win_rate": Decimal("0.28"),
            "total_pnl": Decimal("-5"),
            "directions": {
                "short": {"sample_count": 14, "win_rate": Decimal("0.28"), "total_pnl": Decimal("-5")},
            },
            "current_guard": {
                "sample_count": 4,
                "wins": 4,
                "losses": 0,
                "win_rate": Decimal("1"),
                "total_pnl": Decimal("3"),
            },
        },
    )

    assert status["performance_adjustment_scope"] == "current_guard"
    assert status["performance_entry_penalty"] == Decimal("0")
    assert status["directional_performance_entry_penalty"]["short"] == Decimal("0")
    assert status["short_entry"]["threshold"] == Decimal("2.820")


def test_v2_entry_penalty_bootstraps_current_guard_before_enough_samples(tmp_path):
    cfg = settings(tmp_path, OPEN_MIN_EDGE=Decimal("2.40"), MT4_SLIPPAGE_POINTS=0)
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
        metrics=PositionMetrics(),
        realized_performance={
            "sample_count": 6,
            "wins": 0,
            "losses": 6,
            "win_rate": Decimal("0"),
            "total_pnl": Decimal("-6"),
            "directions": {
                "short": {"sample_count": 6, "win_rate": Decimal("0"), "total_pnl": Decimal("-6")},
            },
            "current_guard": {
                "sample_count": 2,
                "wins": 2,
                "losses": 0,
                "win_rate": Decimal("1"),
                "total_pnl": Decimal("2"),
            },
        },
    )

    assert status["performance_adjustment_scope"] == "current_guard_bootstrap"
    assert status["performance_entry_penalty"] == Decimal("0")
    assert status["short_entry"]["threshold"] == Decimal("2.820")


def test_v2_directional_performance_penalty_only_hits_losing_side(tmp_path):
    cfg = settings(tmp_path, OPEN_MIN_EDGE=Decimal("2.40"), MT4_SLIPPAGE_POINTS=0)
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
        metrics=PositionMetrics(),
        realized_performance={
            "sample_count": 6,
            "wins": 3,
            "losses": 3,
            "win_rate": Decimal("0.5"),
            "target_win_rate": Decimal("0.70"),
            "total_pnl": Decimal("-3"),
            "average_pnl": Decimal("-0.5"),
            "latest_pnl": Decimal("-1"),
            "min_pnl": Decimal("-2"),
            "max_pnl": Decimal("1"),
            "reason": "总表现不达标",
            "directions": {
                "short": {
                    "sample_count": 3,
                    "wins": 0,
                    "losses": 3,
                    "win_rate": Decimal("0"),
                    "total_pnl": Decimal("-4"),
                },
                "long": {
                    "sample_count": 0,
                    "reason": "做多方向暂无 V2 真实做单历史。",
                },
            },
        },
    )

    assert status["directional_performance_entry_penalty"]["short"] == Decimal("0.50")
    assert status["directional_performance_entry_penalty"]["long"] == Decimal("0")
    assert status["short_entry"]["threshold"] == Decimal("2.820")
    assert status["long_entry"]["threshold"] == Decimal("2.4")
    assert status["objective_health"]["realized_ok"] is False
    assert status["objective_health"]["ready_for_goal"] is False
    assert "真实闭环胜率" in status["objective_health"]["reason"]


def test_v2_objective_health_reports_ready_only_when_real_and_projected_targets_pass():
    health = _objective_health(
        realized_performance={"sample_count": 5, "win_rate": Decimal("0.8")},
        short_model={"selected": {"projected_daily_trades": Decimal("3.2")}},
        long_model={"selected": {"projected_daily_trades": Decimal("2.1")}},
        short_threshold=Decimal("3.8"),
        long_threshold=Decimal("2.4"),
    )

    assert health["realized_ok"] is True
    assert health["projected_ok"] is True
    assert health["ready_for_goal"] is True
    assert health["target_daily_trades_min"] == Decimal("3")
    assert health["target_daily_trades_max"] == Decimal("5")


def test_v2_objective_health_uses_current_guard_samples_when_available():
    health = _objective_health(
        realized_performance={
            "sample_count": 14,
            "win_rate": Decimal("0.28"),
            "current_guard": {
                "sample_count": 5,
                "win_rate": Decimal("0.8"),
                "version": "guard",
                "start_ms": 1_000,
            },
        },
        short_model={"selected": {"projected_daily_trades": Decimal("3.2")}},
        long_model={"selected": {"projected_daily_trades": Decimal("2.1")}},
        short_threshold=Decimal("3.8"),
        long_threshold=Decimal("2.4"),
    )

    assert health["realized_ok"] is True
    assert health["ready_for_goal"] is True
    assert health["realized_sample_count"] == 5
    assert health["current_guard_sample_count"] == 5
    assert health["overall_realized_sample_count"] == 14
    assert health["current_guard_version"] == "guard"


def test_v2_objective_health_blocks_until_current_guard_has_samples():
    health = _objective_health(
        realized_performance={
            "sample_count": 14,
            "win_rate": Decimal("0.8"),
            "current_guard": {
                "sample_count": 0,
                "version": "guard",
                "start_ms": 1_000,
            },
        },
        short_model={"selected": {"projected_daily_trades": Decimal("3.2")}},
        long_model={"selected": {"projected_daily_trades": Decimal("2.1")}},
        short_threshold=Decimal("3.8"),
        long_threshold=Decimal("2.4"),
    )

    assert health["realized_ok"] is False
    assert health["ready_for_goal"] is False
    assert health["current_guard_sample_count"] == 0
    assert "当前保护版" in health["reason"]


def test_v2_performance_penalty_keeps_daily_trade_cap():
    model = {
        "candidates": [
            {"threshold": Decimal("3.72"), "projected_daily_trades": Decimal("3.26")},
            {"threshold": Decimal("3.95"), "projected_daily_trades": Decimal("3.02")},
            {"threshold": Decimal("4.10"), "projected_daily_trades": Decimal("1.60")},
        ]
    }

    threshold = _threshold_with_performance_penalty(Decimal("3.72"), Decimal("0.50"), model)

    assert threshold == Decimal("3.95")


def test_v2_realized_positive_total_uses_small_entry_penalty(tmp_path):
    cfg = settings(tmp_path, OPEN_MIN_EDGE=Decimal("2.40"), MT4_SLIPPAGE_POINTS=0)
    store = Storage(cfg.sqlite_path)
    for pnl in ["1.0", "0.5", "-0.2"]:
        store.record_event("v2_pair_pnl_recorded", {"realized_pnl": pnl})
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
        metrics=PositionMetrics(),
    )

    assert status["realized_performance"]["win_rate"] == Decimal("0.6666666666666666666666666667")
    assert status["performance_entry_penalty"] == Decimal("0.0333333333333333333333333333")
    assert status["short_entry"]["threshold"] == Decimal("2.820")


def test_v2_blocks_entry_when_next_triple_swap_makes_exit_unsafe(tmp_path):
    next_rollover_ms = utc_now_ms() + 10 * 60_000
    next_rollover_weekday = datetime.fromtimestamp(next_rollover_ms / 1000, timezone.utc).weekday()
    cfg = settings(
        tmp_path,
        CLOSE_PROFIT_USD_PER_OZ=Decimal("0.20"),
        MT4_TRIPLE_SWAP_WEEKDAY=next_rollover_weekday,
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
            mt4_next_rollover_time_ms=next_rollover_ms,
        ),
    )

    assert status["short_entry"]["current_edge"] >= status["short_entry"]["threshold"]
    assert status["short_entry"]["next_settlement_adjustment"]["mt4_swap"] == Decimal("-1.9788")
    assert status["short_entry"]["estimated_exit_target_spread"] == Decimal("0.8212")
    assert status["short_entry"]["exit_viable"] is False
    assert status["short_entry"]["ready"] is False
    assert "隔夜费" in status["short_entry"]["reason"]


def test_v2_entry_ignores_settlement_outside_expected_cycle(tmp_path):
    cfg = settings(
        tmp_path,
        CLOSE_PROFIT_USD_PER_OZ=Decimal("0.20"),
        MAX_PAIR_AGE_MINUTES=15,
        MT4_TRIPLE_SWAP_MULTIPLIER=Decimal("3"),
    )
    store = Storage(cfg.sqlite_path)
    mt4_bars, binance_bars = recent_bars([Decimal("1.0"), Decimal("2.6"), Decimal("2.8"), Decimal("3.0")] * 3)
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
            binance_funding_rate=Decimal("0.001"),
            binance_next_funding_time_ms=utc_now_ms() + 8 * 60 * 60_000,
            mt4_swap_long_per_lot=Decimal("-65.96"),
            mt4_swap_short_per_lot=Decimal("27.09"),
            mt4_swap_type=0,
            mt4_next_rollover_time_ms=utc_now_ms() + 2 * 60 * 60_000,
        ),
    )

    assert status["short_entry"]["next_settlement_adjustment"] is None
    assert status["short_entry"]["entry_viability_close_profit_usd_per_oz"] == Decimal("0.10")


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
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4003.0"), ask=Decimal("4003.2")),
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


def test_v2_exit_follow_budget_uses_learned_mt4_close_slippage(tmp_path, monkeypatch):
    cfg = settings(tmp_path, MT4_CLOSE_EXTRA_BUFFER_USD=Decimal("0"))
    store = Storage(cfg.sqlite_path)
    monkeypatch.setattr(v2_planner, "GOLD_V2_CURRENT_GUARD_START_MS", utc_now_ms() - 1_000)
    store.record_event("v2_pair_pnl_recorded", {"mt4_close_adverse_slippage": "0.95"})
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

    assert status["mt4_exit_follow_budget"] == Decimal("0.95")
    assert status["short_entry"]["mt4_exit_follow_budget"] == Decimal("0.95")


def test_v2_entry_slippage_budget_uses_learned_mt4_entry_slippage(tmp_path, monkeypatch):
    cfg = settings(tmp_path, MT4_SLIPPAGE_POINTS=0)
    store = Storage(cfg.sqlite_path)
    monkeypatch.setattr(v2_planner, "GOLD_V2_CURRENT_GUARD_START_MS", utc_now_ms() - 1_000)
    store.record_event("v2_mt4_entry_slippage", {"mt4_entry_adverse_slippage": "1.10"})
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

    assert status["mt4_slippage_budget"] == Decimal("1.10")
    assert status["short_entry"]["mt4_slippage_budget"] == Decimal("1.10")


def test_v2_entry_slippage_budget_ignores_old_guard_slippage(tmp_path, monkeypatch):
    cfg = settings(tmp_path, MT4_SLIPPAGE_POINTS=0)
    store = Storage(cfg.sqlite_path)
    now = utc_now_ms()
    monkeypatch.setattr(v2_planner, "GOLD_V2_CURRENT_GUARD_START_MS", now + 60_000)
    store.record_event("v2_mt4_entry_slippage", {"mt4_entry_adverse_slippage": "1.10"})
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

    assert status["mt4_slippage_budget"] == Decimal("0.3")
    assert status["short_entry"]["mt4_slippage_budget"] == Decimal("0.3")


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
    assert status["add_plan"]["raw_next_trigger_edge"] == Decimal("4.8")
    assert status["add_plan"]["next_trigger_edge"] == Decimal("4.00")
    assert status["add_plan"]["trigger_cap_applied"] is True
    assert status["add_plan"]["ready"] is False


def test_v2_add_plan_blocks_above_four_dollar_trigger(tmp_path):
    cfg = settings(tmp_path, ADD_EDGE_GROWTH_USD=Decimal("1"), MT4_SLIPPAGE_POINTS=0, MT4_CLOSE_EXTRA_BUFFER_USD=Decimal("0"))
    store = Storage(cfg.sqlite_path)
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("2"),
        binance_entry_price=Decimal("4059.61"),
        mt4_entry_price=Decimal("4057.96"),
        binance_order_id="entry/add",
        base_edge=Decimal("2.08"),
        add_count=1,
    )

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4003.9"), ask=Decimal("4004.1")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("3999.8"), ask=Decimal("4000.0")),
        binance_bars=[],
        open_pair=pair,
        metrics=PositionMetrics(actual_entry_spread=Decimal("1.65"), close_profit_usd_per_oz=Decimal("0.10")),
        mt4_tick_move_budget=Decimal("0"),
    )

    add_plan = status["add_plan"]
    assert add_plan["raw_next_trigger_edge"] == Decimal("4.08")
    assert add_plan["next_trigger_edge"] == Decimal("4.00")
    assert add_plan["current_edge"] == Decimal("4.1")
    assert add_plan["next_actionable_trigger_edge"] == Decimal("3.70")
    assert add_plan["ready"] is False
    assert "超过黄金正常上限 4.00" in add_plan["reason"]
    assert add_plan["estimated_blended_edge"] is None


def test_v2_add_plan_does_not_lower_current_average_edge(tmp_path):
    cfg = settings(tmp_path, ADD_EDGE_GROWTH_USD=Decimal("1"), MT4_SLIPPAGE_POINTS=37)
    store = Storage(cfg.sqlite_path)
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("2"),
        binance_entry_price=Decimal("4030.76"),
        mt4_entry_price=Decimal("4022.49"),
        binance_order_id="entry/add",
        base_edge=Decimal("2.00"),
        add_count=1,
    )

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4020.5"), ask=Decimal("4020.7")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("4016.4"), ask=Decimal("4016.7")),
        binance_bars=[],
        open_pair=pair,
        metrics=PositionMetrics(actual_entry_spread=Decimal("8.27")),
        mt4_tick_move_budget=Decimal("0.37"),
    )

    add_plan = status["add_plan"]
    assert add_plan["next_trigger_edge"] == Decimal("4.00")
    assert add_plan["average_protection_edge"] == Decimal("8.27")
    assert add_plan["add_improvement_buffer"] == Decimal("0.52")
    assert add_plan["required_average_after_add"] == Decimal("8.79")
    assert add_plan["next_actionable_trigger_edge"] == Decimal("8.79")
    assert add_plan["current_edge"] >= add_plan["next_trigger_edge"]
    assert add_plan["current_edge"] < add_plan["next_actionable_trigger_edge"]
    assert add_plan["estimated_blended_edge"] >= add_plan["required_average_after_add"]
    assert add_plan["ready"] is False
    assert add_plan["binance_price"] - Decimal("4016.7") >= add_plan["next_actionable_trigger_edge"]


def test_v2_add_plan_requires_visible_average_edge_improvement(tmp_path):
    cfg = settings(tmp_path, ADD_EDGE_GROWTH_USD=Decimal("1"), MT4_SLIPPAGE_POINTS=0)
    store = Storage(cfg.sqlite_path)
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("10"),
        binance_entry_price=Decimal("4002"),
        mt4_entry_price=Decimal("4000"),
        binance_order_id="entry/add",
        base_edge=Decimal("2.00"),
    )

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4003.3"), ask=Decimal("4003.5")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("3999.8"), ask=Decimal("4000.0")),
        binance_bars=[],
        open_pair=pair,
        metrics=PositionMetrics(actual_entry_spread=Decimal("2.00")),
        mt4_tick_move_budget=Decimal("0"),
    )

    add_plan = status["add_plan"]
    assert add_plan["current_edge"] > add_plan["next_trigger_edge"]
    assert add_plan["next_actionable_trigger_edge"] == Decimal("3.90")
    assert add_plan["required_average_after_add"] == Decimal("2.20")
    assert add_plan["current_edge"] < add_plan["next_actionable_trigger_edge"]
    assert add_plan["ready"] is False
    assert "保护触发位" in add_plan["reason"]


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
    assert status["add_plan"]["expected_locked_edge"] >= status["add_plan"]["required_locked_edge"]


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


def test_v2_stale_weak_pair_can_exit_within_loss_limit(tmp_path):
    cfg = settings(tmp_path, OPEN_MIN_EDGE=Decimal("2.40"), MAX_PAIR_AGE_MINUTES=60, MAX_PAIR_LOSS_USDT=Decimal("5"))
    store = Storage(cfg.sqlite_path)
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("2"),
        binance_entry_price=Decimal("4059.61"),
        mt4_entry_price=Decimal("4057.96"),
        binance_order_id="entry/add",
        base_edge=Decimal("1.65"),
        opened_ms=utc_now_ms() - 61 * 60_000,
    )
    metrics = PositionMetrics(
        actual_entry_spread=Decimal("1.65"),
        current_exit_spread=Decimal("2.80"),
        profitable_spread_threshold=Decimal("1.34"),
        dynamic_close_spread=Decimal("0.64"),
        estimated_close_net=Decimal("-3.00"),
    )

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4002.6"), ask=Decimal("4002.8")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("3999.8"), ask=Decimal("4000.0")),
        binance_bars=[],
        open_pair=pair,
        metrics=metrics,
    )

    exit_plan = status["exit_plan"]
    assert exit_plan["stale_weak"]["active"] is True
    assert exit_plan["target_exit_spread"] == Decimal("3.84")
    assert exit_plan["reason"].startswith("低质量旧仓已超过")


def test_v2_stale_weak_pair_waits_before_max_age(tmp_path):
    cfg = settings(tmp_path, OPEN_MIN_EDGE=Decimal("2.40"), MAX_PAIR_AGE_MINUTES=60, MAX_PAIR_LOSS_USDT=Decimal("5"))
    store = Storage(cfg.sqlite_path)
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("2"),
        binance_entry_price=Decimal("4059.61"),
        mt4_entry_price=Decimal("4057.96"),
        binance_order_id="entry/add",
        base_edge=Decimal("1.65"),
        opened_ms=utc_now_ms() - 30 * 60_000,
    )
    metrics = PositionMetrics(
        actual_entry_spread=Decimal("1.65"),
        current_exit_spread=Decimal("2.80"),
        profitable_spread_threshold=Decimal("1.34"),
        dynamic_close_spread=Decimal("0.64"),
        estimated_close_net=Decimal("-3.00"),
    )

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4002.6"), ask=Decimal("4002.8")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("3999.8"), ask=Decimal("4000.0")),
        binance_bars=[],
        open_pair=pair,
        metrics=metrics,
    )

    assert status["exit_plan"]["stale_weak"]["active"] is False
    assert status["exit_plan"]["target_exit_spread"] == Decimal("0.64")


def test_v2_critical_weak_pair_exits_without_waiting_for_max_age(tmp_path):
    cfg = settings(
        tmp_path,
        OPEN_MIN_EDGE=Decimal("2.40"),
        CLOSE_PROFIT_USD_PER_OZ=Decimal("1.21"),
        AGED_CLOSE_PROFIT_USD_PER_OZ=Decimal("0.10"),
        MAX_PAIR_AGE_MINUTES=60,
        MAX_PAIR_LOSS_USDT=Decimal("5"),
    )
    store = Storage(cfg.sqlite_path)
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("4000.50"),
        mt4_entry_price=Decimal("4000.00"),
        binance_order_id="entry",
        opened_ms=utc_now_ms() - 5 * 60_000,
    )

    status = build_gold_v2_status(
        settings=cfg,
        storage=store,
        filters=filters(),
        binance_quote=MarketQuote(symbol="XAUUSDT", bid=Decimal("4001"), ask=Decimal("4001.1")),
        mt4_quote=MarketQuote(symbol="XAUUSD", bid=Decimal("3999.5"), ask=Decimal("3999.8")),
        binance_bars=[],
        open_pair=pair,
        metrics=PositionMetrics(
            actual_entry_spread=Decimal("0.50"),
            current_exit_spread=Decimal("1.5"),
            profitable_spread_threshold=Decimal("0.50"),
            dynamic_close_spread=Decimal("0"),
            estimated_close_net=Decimal("-1.0"),
            exit_follow_buffer_usd_per_oz=Decimal("0.95"),
            close_profit_usd_per_oz=Decimal("0.10"),
        ),
    )

    stale_weak = status["exit_plan"]["stale_weak"]
    assert stale_weak["active"] is True
    assert stale_weak["critical_min_edge"] == Decimal("1.05")
    assert stale_weak["target_exit_spread"] == Decimal("5.50")
    assert status["exit_plan"]["target_exit_spread"] == Decimal("5.50")
    assert "严重低质量进场" in status["exit_plan"]["reason"]


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
