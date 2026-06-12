from decimal import Decimal

from app.history import compare_spreads
from app.models import HistoryBar
from app.storage import Storage


def bar(ts: int, close: str) -> HistoryBar:
    value = Decimal(close)
    return HistoryBar(open_time_ms=ts, open=value, high=value, low=value, close=value)


def test_storage_upserts_and_reads_history_bars(tmp_path):
    store = Storage(tmp_path / "test.sqlite3")
    rows = [bar(1000, "4170.10"), bar(2000, "4171.20")]

    assert store.upsert_bars("mt4", "XAUUSD", "1m", rows) == 2
    assert store.upsert_bars("mt4", "XAUUSD", "1m", [bar(2000, "4171.50")]) == 1

    saved = store.get_bars("mt4", "XAUUSD", "1m", 0, 3000)
    assert len(saved) == 2
    assert saved[-1].close == Decimal("4171.50")
    assert store.bar_count("mt4", "XAUUSD", "1m") == 2


def test_spread_analysis_detects_return_to_threshold():
    mt4 = [bar(1000, "4170"), bar(2000, "4171"), bar(3000, "4172")]
    binance = [bar(1000, "4173"), bar(2000, "4171.40"), bar(3000, "4175")]

    result = compare_spreads(mt4, binance, days=7, interval="1m", threshold=Decimal("0.50"))

    assert result.ready
    assert result.returned_to_threshold
    assert result.return_count == 1
    assert result.min_abs_diff == Decimal("0.40")
    assert result.min_abs_diff_time_ms == 2000
    assert result.latest_diff == Decimal("3")


def test_spread_analysis_reports_unaligned_bars():
    result = compare_spreads(
        [bar(1000, "4170")],
        [bar(2000, "4173")],
        days=7,
        interval="1m",
        threshold=Decimal("0.50"),
    )

    assert not result.ready
    assert result.reason == "MT4 和 Binance 的K线时间没有对齐"
