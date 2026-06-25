from decimal import Decimal
from datetime import datetime, timezone
from types import SimpleNamespace

import app.main as main
import app.mt4_rollover as mt4_rollover
from app.models import OpenPair, PairDirection
from app.main import (
    _binance_transient_cooldown_ms,
    _dynamic_close_spread,
    _effective_close_profit_usd_per_oz,
    _exit_follow_buffer_usd_per_oz,
    _estimate_mt4_swap,
    _immediate_close_net,
    _projected_close_net_after_next_settlement,
)


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


def test_exit_follow_buffer_uses_normal_follow_window_not_timeout(monkeypatch):
    calls = []

    def fake_recent_move_budget(lookback_ms, percentile, min_points):
        calls.append((lookback_ms, percentile, min_points))
        return Decimal("0.20")

    monkeypatch.setattr(main.settings, "max_hedge_delay_ms", 10_000)
    monkeypatch.setattr(main.settings, "mt4_slippage_points", 0)
    monkeypatch.setattr(main.settings, "mt4_close_extra_buffer_usd", Decimal("0.80"))
    monkeypatch.setattr(main.mt4_bridge, "recent_move_budget", fake_recent_move_budget)
    swap_info = SimpleNamespace(point=Decimal("0.01"))

    assert _exit_follow_buffer_usd_per_oz(swap_info) == Decimal("1.00")
    assert calls[0][0] == 1000


def test_exit_follow_buffer_does_not_fallback_to_slow_bar_move(monkeypatch):
    monkeypatch.setattr(main.settings, "max_hedge_delay_ms", 10_000)
    monkeypatch.setattr(main.settings, "mt4_slippage_points", 0)
    monkeypatch.setattr(main.settings, "mt4_close_extra_buffer_usd", Decimal("0.80"))
    monkeypatch.setattr(main.mt4_bridge, "recent_move_budget", lambda *args, **kwargs: None)
    swap_info = SimpleNamespace(point=Decimal("0.01"))

    assert _exit_follow_buffer_usd_per_oz(swap_info, mt4_bars=[]) == Decimal("0.80")


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
