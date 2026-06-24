from decimal import Decimal
from datetime import datetime, timezone
from types import SimpleNamespace

import app.main as main
from app.models import OpenPair, PairDirection
from app.main import (
    _dynamic_close_spread,
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


def test_dynamic_close_spread_does_not_go_negative_when_buffer_is_large():
    assert _dynamic_close_spread(
        profitable_spread_threshold=Decimal("2.52"),
        exit_follow_buffer=Decimal("2.70"),
        close_profit=Decimal("0.20"),
    ) == Decimal("0")


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
        next_rollover_time_ms=int(datetime(2026, 6, 24, 20, 59, tzinfo=timezone.utc).timestamp() * 1000),
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
        next_rollover_time_ms=int(datetime(2026, 6, 25, 20, 59, tzinfo=timezone.utc).timestamp() * 1000),
    )

    assert _estimate_mt4_swap(pair, Decimal("1"), swap_info) == Decimal("-0.6596")
