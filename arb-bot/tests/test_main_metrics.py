from decimal import Decimal
from datetime import datetime, timezone
from types import SimpleNamespace

import app.main as main
import app.mt4_rollover as mt4_rollover
from app.models import OpenPair, PairDirection, StrategyState, TradeHistoryItem
from app.main import (
    _binance_transient_cooldown_ms,
    _dynamic_close_spread,
    _effective_close_profit_usd_per_oz,
    _exit_follow_buffer_usd_per_oz,
    _estimate_mt4_swap,
    _gold_v2_realized_performance_from_items,
    _immediate_close_net,
    _live_mismatch_wait_state,
    _projected_close_net_after_next_settlement,
)


def event_at(timestamp_ms: int, kind: str, payload: dict) -> dict:
    return {
        "ts": datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).isoformat(),
        "kind": kind,
        "payload": payload,
    }


def test_live_mismatch_wait_state_without_pair_stays_idle(monkeypatch):
    monkeypatch.setattr(main.strategy, "open_pair", None)

    assert _live_mismatch_wait_state() == StrategyState.IDLE


def test_live_mismatch_wait_state_with_pair_keeps_pair_open(monkeypatch):
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("101"),
        mt4_entry_price=Decimal("99"),
        binance_order_id="entry",
    )
    monkeypatch.setattr(main.strategy, "open_pair", pair)

    assert _live_mismatch_wait_state() == StrategyState.PAIR_OPEN


def test_immediate_close_net_excludes_future_funding_and_swap():
    immediate = _immediate_close_net(
        gross=Decimal("1.00"),
        fees=Decimal("0.10"),
        accrued_funding=Decimal("0.20"),
        accrued_swap=Decimal("-0.05"),
        mt4_spread_protection=Decimal("0.30"),
    )

    assert immediate == Decimal("0.75")
    assert _projected_close_net_after_next_settlement(
        immediate,
        funding_estimate=Decimal("0.40"),
        mt4_swap_estimate=Decimal("-0.60"),
    ) == Decimal("0.55")


def test_gold_v2_execution_quality_uses_current_guard_events(monkeypatch):
    guard = main.GOLD_V2_CURRENT_GUARD_START_MS
    fake_storage = SimpleNamespace(
        get_events=lambda start_ms, end_ms, limit=5000: [
            event_at(
                guard - 1_000,
                "v2_mt4_entry_slippage",
                {"mt4_entry_adverse_slippage": "99", "mt4_command_to_report_latency_ms": "999"},
            ),
            event_at(
                guard + 1_000,
                "v2_mt4_entry_slippage",
                {"mt4_entry_adverse_slippage": "0.20", "mt4_command_to_report_latency_ms": "120"},
            ),
            event_at(
                guard + 2_000,
                "v2_mt4_add_slippage",
                {"mt4_entry_adverse_slippage": "-0.10", "mt4_command_to_report_latency_ms": "80"},
            ),
            event_at(
                guard + 3_000,
                "v2_pair_pnl_recorded",
                {
                    "realized_pnl": "1.50",
                    "entry_spread": "3.00",
                    "actual_exit_spread": "1.20",
                    "mt4_close_adverse_slippage": "0.15",
                    "mt4_command_to_report_latency_ms": "90",
                },
            ),
            event_at(
                guard + 4_000,
                "v2_binance_fee_or_taker_detected",
                {"phase": "entry", "commission": "0.01", "all_maker": False},
            ),
        ]
    )
    monkeypatch.setattr(main, "storage", fake_storage)

    quality = main._gold_v2_execution_quality(now_ms=guard + 10_000)

    assert quality["ready"] is True
    assert quality["entry_follow"]["sample_count"] == 2
    assert quality["entry_follow"]["average"] == Decimal("0.05")
    assert quality["entry_follow"]["max"] == Decimal("0.20")
    assert quality["entry_follow_latency_ms"]["average"] == Decimal("100")
    assert quality["closed_pairs"]["sample_count"] == 1
    assert quality["closed_pairs"]["win_rate"] == Decimal("1")
    assert quality["closed_pairs"]["total"] == Decimal("1.50")
    assert quality["spread_capture_usd_per_oz"]["latest"] == Decimal("1.80")
    assert quality["close_follow"]["latest"] == Decimal("0.15")
    assert quality["binance_fee_or_taker_event_count"] == 1


def test_gold_v2_realized_performance_uses_true_v2_trade_history_only():
    performance = _gold_v2_realized_performance_from_items(
        [
            TradeHistoryItem(strategy_version="v2.0", binance_entry_side=main.Side.SELL, net_pnl=Decimal("-2.0"), status="真实"),
            TradeHistoryItem(strategy_version="v1.0", binance_entry_side=main.Side.SELL, net_pnl=Decimal("100.0"), status="旧版"),
            TradeHistoryItem(strategy_version="v2.0", binance_entry_side=main.Side.BUY, net_pnl=Decimal("1.5"), status="真实"),
            TradeHistoryItem(strategy_version="v2.0", binance_entry_side=main.Side.SELL, net_pnl=None, status="缺少币安成交匹配"),
            TradeHistoryItem(strategy_version="v2.0", binance_entry_side=main.Side.SELL, net_pnl=Decimal("0"), status="真实"),
        ]
    )

    assert performance["sample_count"] == 3
    assert performance["wins"] == 1
    assert performance["losses"] == 1
    assert performance["win_rate"] == Decimal("0.3333333333333333333333333333")
    assert performance["total_pnl"] == Decimal("-0.5")
    assert performance["latest_pnl"] == Decimal("-2.0")
    assert "自动抬高" in performance["reason"]
    assert performance["directions"]["short"]["sample_count"] == 2
    assert performance["directions"]["short"]["total_pnl"] == Decimal("-2.0")
    assert performance["directions"]["long"]["sample_count"] == 1
    assert performance["directions"]["long"]["win_rate"] == Decimal("1")
    assert performance["current_guard"]["sample_count"] == 0
    assert performance["current_guard"]["version"] == main.GOLD_V2_CURRENT_GUARD_VERSION


def test_gold_v2_realized_performance_splits_current_guard_history():
    guard_start = main.GOLD_V2_CURRENT_GUARD_START_MS
    performance = _gold_v2_realized_performance_from_items(
        [
            TradeHistoryItem(
                strategy_version="v2.0",
                open_time_ms=guard_start - 60_000,
                binance_entry_side=main.Side.SELL,
                net_pnl=Decimal("-5.0"),
                status="旧保护",
            ),
            TradeHistoryItem(
                strategy_version="v2.0",
                open_time_ms=guard_start,
                binance_entry_side=main.Side.SELL,
                net_pnl=Decimal("1.0"),
                status="当前保护",
            ),
            TradeHistoryItem(
                strategy_version="v2.0",
                close_time_ms=guard_start + 60_000,
                binance_entry_side=main.Side.BUY,
                net_pnl=Decimal("-0.5"),
                status="当前保护",
            ),
        ]
    )

    assert performance["sample_count"] == 3
    assert performance["total_pnl"] == Decimal("-4.5")
    assert performance["current_guard"]["sample_count"] == 2
    assert performance["current_guard"]["total_pnl"] == Decimal("0.5")
    assert performance["current_guard"]["directions"]["short"]["sample_count"] == 1
    assert performance["current_guard"]["directions"]["long"]["sample_count"] == 1


def test_gold_v2_realized_performance_handles_empty_history():
    performance = _gold_v2_realized_performance_from_items(
        [TradeHistoryItem(strategy_version="v1.0", net_pnl=Decimal("-10"), status="旧版")]
    )

    assert performance["sample_count"] == 0
    assert "暂无 V2" in performance["reason"]
    assert performance["directions"]["short"]["sample_count"] == 0


def test_binance_too_many_requests_uses_long_cooldown():
    assert _binance_transient_cooldown_ms('{"code":-1003,"msg":"Too many requests"}') == 300_000


def test_dynamic_close_spread_subtracts_exit_buffer_once():
    assert _dynamic_close_spread(
        profitable_spread_threshold=Decimal("2.52"),
        exit_follow_buffer=Decimal("2.70"),
        close_profit=Decimal("0.20"),
    ) == Decimal("0")


def test_dynamic_close_spread_keeps_positive_target_after_one_buffer():
    assert _dynamic_close_spread(
        profitable_spread_threshold=Decimal("2.52"),
        exit_follow_buffer=Decimal("0.60"),
        close_profit=Decimal("0.20"),
    ) == Decimal("1.72")


def test_dynamic_close_spread_does_not_go_negative_when_profit_exceeds_threshold():
    assert _dynamic_close_spread(
        profitable_spread_threshold=Decimal("0.10"),
        exit_follow_buffer=Decimal("2.70"),
        close_profit=Decimal("0.20"),
    ) == Decimal("0")


def test_effective_close_profit_relaxes_positive_target_after_timeout(monkeypatch):
    now_ms = 1_000_000
    monkeypatch.setattr(main, "utc_now_ms", lambda: now_ms)
    monkeypatch.setattr(main.settings, "max_pair_age_minutes", 10)
    monkeypatch.setattr(main.settings, "close_profit_usd_per_oz", Decimal("0.20"))
    monkeypatch.setattr(main.settings, "aged_close_profit_usd_per_oz", Decimal("0.01"))
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("4000"),
        mt4_entry_price=Decimal("3998"),
        binance_order_id="entry",
        opened_ms=now_ms - 11 * 60_000,
    )

    assert _effective_close_profit_usd_per_oz(pair) == Decimal("0.01")


def test_effective_close_profit_relaxes_weak_entry_before_timeout(monkeypatch):
    now_ms = 1_000_000
    monkeypatch.setattr(main, "utc_now_ms", lambda: now_ms)
    monkeypatch.setattr(main.settings, "open_min_edge", Decimal("2.40"))
    monkeypatch.setattr(main.settings, "max_pair_age_minutes", 60)
    monkeypatch.setattr(main.settings, "close_profit_usd_per_oz", Decimal("1.21"))
    monkeypatch.setattr(main.settings, "aged_close_profit_usd_per_oz", Decimal("0.10"))
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("4000"),
        mt4_entry_price=Decimal("3998.35"),
        binance_order_id="entry",
        opened_ms=now_ms - 5 * 60_000,
    )

    assert _effective_close_profit_usd_per_oz(pair, actual_entry_spread=Decimal("1.65")) == Decimal("0.10")


def test_effective_close_profit_keeps_normal_target_for_good_entry_before_timeout(monkeypatch):
    now_ms = 1_000_000
    monkeypatch.setattr(main, "utc_now_ms", lambda: now_ms)
    monkeypatch.setattr(main.settings, "open_min_edge", Decimal("2.40"))
    monkeypatch.setattr(main.settings, "max_pair_age_minutes", 60)
    monkeypatch.setattr(main.settings, "close_profit_usd_per_oz", Decimal("1.21"))
    monkeypatch.setattr(main.settings, "aged_close_profit_usd_per_oz", Decimal("0.10"))
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("4003"),
        mt4_entry_price=Decimal("4000"),
        binance_order_id="entry",
        opened_ms=now_ms - 5 * 60_000,
    )

    assert _effective_close_profit_usd_per_oz(pair, actual_entry_spread=Decimal("3.00")) == Decimal("1.21")


def test_exit_follow_buffer_uses_normal_follow_window_not_timeout(monkeypatch):
    calls = []

    def fake_recent_move_budget(lookback_ms, percentile, min_points):
        calls.append((lookback_ms, percentile, min_points))
        return Decimal("0.20")

    monkeypatch.setattr(main.settings, "max_hedge_delay_ms", 10_000)
    monkeypatch.setattr(main.settings, "mt4_slippage_points", 0)
    monkeypatch.setattr(main.settings, "mt4_close_extra_buffer_usd", Decimal("0.80"))
    monkeypatch.setattr(main.mt4_bridge, "recent_move_budget", fake_recent_move_budget)
    monkeypatch.setattr(main, "mt4_close_slippage_budget_usd_per_oz", lambda *args, **kwargs: Decimal("0"))
    swap_info = SimpleNamespace(point=Decimal("0.01"))

    assert _exit_follow_buffer_usd_per_oz(swap_info) == Decimal("1.00")
    assert calls[0][0] == 1000


def test_exit_follow_buffer_does_not_fallback_to_slow_bar_move(monkeypatch):
    monkeypatch.setattr(main.settings, "max_hedge_delay_ms", 10_000)
    monkeypatch.setattr(main.settings, "mt4_slippage_points", 0)
    monkeypatch.setattr(main.settings, "mt4_close_extra_buffer_usd", Decimal("0.80"))
    monkeypatch.setattr(main.mt4_bridge, "recent_move_budget", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "mt4_close_slippage_budget_usd_per_oz", lambda *args, **kwargs: Decimal("0"))
    swap_info = SimpleNamespace(point=Decimal("0.01"))

    assert _exit_follow_buffer_usd_per_oz(swap_info, mt4_bars=[]) == Decimal("0.80")


def test_exit_follow_buffer_uses_learned_mt4_close_slippage(monkeypatch):
    monkeypatch.setattr(main.settings, "max_hedge_delay_ms", 10_000)
    monkeypatch.setattr(main.settings, "mt4_slippage_points", 0)
    monkeypatch.setattr(main.settings, "mt4_close_extra_buffer_usd", Decimal("0.80"))
    monkeypatch.setattr(main.mt4_bridge, "recent_move_budget", lambda *args, **kwargs: Decimal("0.10"))
    monkeypatch.setattr(main, "mt4_close_slippage_budget_usd_per_oz", lambda *args, **kwargs: Decimal("1.25"))
    swap_info = SimpleNamespace(point=Decimal("0.01"))

    assert _exit_follow_buffer_usd_per_oz(swap_info, mt4_bars=[]) == Decimal("1.25")


def test_mt4_swap_estimate_uses_triple_swap_weekday(monkeypatch):
    monkeypatch.setattr(main.settings, "mt4_lot_size_oz", Decimal("100"))
    monkeypatch.setattr(main.settings, "mt4_triple_swap_weekday", 2)
    monkeypatch.setattr(main.settings, "mt4_triple_swap_multiplier", Decimal("3"))
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("4013.46"),
        mt4_entry_price=Decimal("4010.63"),
        binance_order_id="entry",
    )
    swap_info = SimpleNamespace(
        swap_long_per_lot=Decimal("-65.96"),
        swap_short_per_lot=Decimal("27.09"),
        swap_type=0,
        tick_value=None,
        tick_size=None,
        point=None,
        next_rollover_time_ms=int(datetime(2026, 7, 1, 20, 59, tzinfo=timezone.utc).timestamp() * 1000),
    )

    assert _estimate_mt4_swap(pair, Decimal("1"), swap_info) == Decimal("-1.9788")


def test_mt4_swap_estimate_uses_normal_day(monkeypatch):
    monkeypatch.setattr(main.settings, "mt4_lot_size_oz", Decimal("100"))
    monkeypatch.setattr(main.settings, "mt4_triple_swap_weekday", 2)
    monkeypatch.setattr(main.settings, "mt4_triple_swap_multiplier", Decimal("3"))
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("4013.46"),
        mt4_entry_price=Decimal("4010.63"),
        binance_order_id="entry",
    )
    swap_info = SimpleNamespace(
        swap_long_per_lot=Decimal("-65.96"),
        swap_short_per_lot=Decimal("27.09"),
        swap_type=0,
        tick_value=None,
        tick_size=None,
        point=None,
        next_rollover_time_ms=int(datetime(2026, 7, 2, 20, 59, tzinfo=timezone.utc).timestamp() * 1000),
    )

    assert _estimate_mt4_swap(pair, Decimal("1"), swap_info) == Decimal("-0.6596")


def test_mt4_swap_estimate_rolls_recent_stale_rollover_to_next_day(monkeypatch):
    monkeypatch.setattr(main.settings, "mt4_lot_size_oz", Decimal("100"))
    monkeypatch.setattr(main.settings, "mt4_triple_swap_weekday", 2)
    monkeypatch.setattr(main.settings, "mt4_triple_swap_multiplier", Decimal("3"))
    stale_rollover = int(datetime(2026, 7, 1, 20, 59, tzinfo=timezone.utc).timestamp() * 1000)
    monkeypatch.setattr(main, "utc_now_ms", lambda: stale_rollover + 60_000)
    monkeypatch.setattr(mt4_rollover, "utc_now_ms", lambda: stale_rollover + 60_000)
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("4013.46"),
        mt4_entry_price=Decimal("4010.63"),
        binance_order_id="entry",
    )
    swap_info = SimpleNamespace(
        swap_long_per_lot=Decimal("-65.96"),
        swap_short_per_lot=Decimal("27.09"),
        swap_type=0,
        tick_value=None,
        tick_size=None,
        point=None,
        next_rollover_time_ms=stale_rollover,
    )

    assert _estimate_mt4_swap(pair, Decimal("1"), swap_info) == Decimal("-0.6596")


def test_mt4_swap_estimate_ignores_stale_rollover(monkeypatch):
    monkeypatch.setattr(main.settings, "mt4_lot_size_oz", Decimal("100"))
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("4013.46"),
        mt4_entry_price=Decimal("4010.63"),
        binance_order_id="entry",
    )
    swap_info = SimpleNamespace(
        swap_long_per_lot=Decimal("-65.96"),
        swap_short_per_lot=Decimal("27.09"),
        swap_type=0,
        tick_value=None,
        tick_size=None,
        point=None,
        next_rollover_time_ms=1,
    )

    assert _estimate_mt4_swap(pair, Decimal("1"), swap_info) is None
