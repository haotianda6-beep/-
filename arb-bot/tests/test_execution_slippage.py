from decimal import Decimal

from app.execution_slippage import mt4_close_slippage_budget_usd_per_oz, mt4_entry_slippage_budget_usd_per_oz
from app.models import utc_now_ms
from app.storage import Storage


def test_mt4_close_slippage_budget_reads_recent_positive_values(tmp_path):
    store = Storage(tmp_path / "test.sqlite3")
    now = utc_now_ms()
    store.record_event("v2_pair_pnl_recorded", {"mt4_close_adverse_slippage": "0.20"})
    store.record_event("v2_pair_pnl_recorded", {"mt4_close_adverse_slippage": "-0.10"})
    store.record_event("v2_pair_pnl_recorded", {"mt4_close_adverse_slippage": "0.95"})

    assert mt4_close_slippage_budget_usd_per_oz(store, now + 1000, percentile=80) == Decimal("0.20")
    assert mt4_close_slippage_budget_usd_per_oz(store, now + 1000, percentile=100) == Decimal("0.95")


def test_mt4_close_slippage_budget_caps_extreme_values(tmp_path):
    store = Storage(tmp_path / "test.sqlite3")
    now = utc_now_ms()
    store.record_event("v2_pair_pnl_recorded", {"mt4_close_adverse_slippage": "9.50"})

    assert mt4_close_slippage_budget_usd_per_oz(store, now + 1000, percentile=100) == Decimal("2")


def test_mt4_entry_slippage_budget_reads_entry_and_add_values(tmp_path):
    store = Storage(tmp_path / "test.sqlite3")
    now = utc_now_ms()
    store.record_event("v2_mt4_entry_slippage", {"mt4_entry_adverse_slippage": "0.35"})
    store.record_event("v2_mt4_add_slippage", {"mt4_entry_adverse_slippage": "1.10"})
    store.record_event("v2_mt4_entry_slippage", {"mt4_entry_adverse_slippage": "-0.20"})
    store.record_event("v2_pair_pnl_recorded", {"mt4_close_adverse_slippage": "1.80"})

    assert mt4_entry_slippage_budget_usd_per_oz(store, now + 1000, percentile=100) == Decimal("1.10")
