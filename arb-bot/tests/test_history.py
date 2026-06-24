from decimal import Decimal

import pytest

from app import main as main_module
from app.history import compare_spreads
from app.models import HistoryBar, Mt4ClosedOrder, Side
from app.storage import Storage
from app.main import BINANCE_HISTORY_MAX_WINDOW_MS, _build_trade_history, _fetch_binance_history_rows


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


def test_storage_upserts_and_reads_mt4_closed_orders(tmp_path):
    store = Storage(tmp_path / "test.sqlite3")
    order = Mt4ClosedOrder(
        ticket=1001,
        symbol="XAUUSD",
        side=Side.BUY,
        lots=Decimal("0.01"),
        open_time_ms=1000,
        close_time_ms=2000,
        open_price=Decimal("4298.17"),
        close_price=Decimal("4288.38"),
        profit=Decimal("-9.79"),
        swap=Decimal("0"),
        commission=Decimal("0"),
        magic_number=260612,
    )

    assert store.upsert_mt4_closed_orders([order]) == 1
    saved = store.get_mt4_closed_orders("XAUUSD", 0, 3000)

    assert len(saved) == 1
    assert saved[0].ticket == 1001
    assert saved[0].profit == Decimal("-9.79")


def test_storage_reads_events_in_time_window(tmp_path):
    store = Storage(tmp_path / "test.sqlite3")
    store.record_event("sample", {"order_id": "1"})

    events = store.get_events(0, 4_102_444_800_000)

    assert len(events) == 1
    assert events[0]["kind"] == "sample"
    assert events[0]["payload"]["order_id"] == "1"


def test_storage_reads_most_recent_events_when_limit_is_smaller_than_window(tmp_path):
    store = Storage(tmp_path / "test.sqlite3")
    for index in range(5):
        store.record_event(f"sample_{index}", {"order_id": str(index)})

    events = store.get_events(0, 4_102_444_800_000, limit=2)

    assert [event["kind"] for event in events] == ["sample_3", "sample_4"]


def test_spread_analysis_detects_return_to_threshold():
    mt4 = [bar(60_038, "4170"), bar(120_038, "4171"), bar(180_038, "4172")]
    binance = [bar(60_000, "4173"), bar(120_000, "4171.40"), bar(180_000, "4175")]

    result = compare_spreads(mt4, binance, days=7, interval="1m", threshold=Decimal("0.50"))

    assert result.ready
    assert result.returned_to_threshold
    assert result.return_count == 1
    assert result.min_abs_diff == Decimal("0.40")
    assert result.min_abs_diff_time_ms == 120_000
    assert result.latest_diff == Decimal("3")


def test_spread_analysis_reports_unaligned_bars():
    result = compare_spreads(
        [bar(60_038, "4170")],
        [bar(120_000, "4173")],
        days=7,
        interval="1m",
        threshold=Decimal("0.50"),
    )

    assert not result.ready
    assert result.reason == "MT4 和 Binance 的K线时间没有对齐"


def mt4_order(
    ticket: int,
    open_time_ms: int,
    close_time_ms: int,
    open_price: str,
    close_price: str,
    profit: str,
    swap: str = "0",
) -> Mt4ClosedOrder:
    return Mt4ClosedOrder(
        ticket=ticket,
        symbol="XAUUSD",
        side=Side.BUY,
        lots=Decimal("0.01"),
        open_time_ms=open_time_ms,
        close_time_ms=close_time_ms,
        open_price=Decimal(open_price),
        close_price=Decimal(close_price),
        profit=Decimal(profit),
        swap=Decimal(swap),
        commission=Decimal("0"),
        magic_number=260612,
    )


def binance_trade(order_id: int, side: str, qty: str, price: str, realized: str, time_ms: int) -> dict:
    return {
        "orderId": order_id,
        "side": side,
        "qty": qty,
        "price": price,
        "realizedPnl": realized,
        "commission": "0",
        "time": time_ms,
    }


def test_trade_history_aligns_grouped_exit_with_real_net_pnl():
    items = _build_trade_history(
        [
            mt4_order(76904392, 1782193000000, 1782193640000, "4150.00", "4119.22", "-1.45"),
            mt4_order(76858510, 1782133221000, 1782193644000, "4156.64", "4116.67", "-75.94", "-0.64"),
        ],
        [binance_trade(7445463165, "BUY", "2", "4121.84", "75.64", 1782193645000)],
        [],
    )

    assert len(items) == 1
    assert items[0].binance_exit_order_id == "7445463165"
    assert items[0].net_pnl == Decimal("-2.39")
    assert "真实" in items[0].status
    assert "原因：本单亏损-2.39U" in items[0].status
    assert "MT4盈亏-78.03U" in items[0].status
    assert "币安合约盈亏+75.64U" in items[0].status


def test_trade_history_marks_quantity_mismatch_but_keeps_real_net_pnl():
    items = _build_trade_history(
        [
            mt4_order(76910622, 1782194492000, 1782194928000, "4126.66", "4116.67", "-9.99"),
            mt4_order(76909629, 1782193701000, 1782195003000, "4120.71", "4113.66", "-7.05"),
        ],
        [binance_trade(7447479800, "BUY", "1.002", "4114", "8.96033998", 1782194930000)],
        [],
    )

    assert len(items) == 1
    assert items[0].binance_exit_order_id == "7447479800"
    assert items[0].quantity_oz == Decimal("2.00")
    assert items[0].net_pnl == Decimal("-8.07966002")
    assert "数量不一致" in items[0].status
    assert "币安1.002 XAU / MT4 2 XAU" in items[0].status


def test_trade_history_uses_event_link_when_binance_exit_precedes_manual_mt4_close():
    items = _build_trade_history(
        [
            mt4_order(76914108, 1782197129000, 1782208751000, "4116.94", "4124.67", "7.73"),
            mt4_order(76914311, 1782197247000, 1782208752000, "4112.45", "4124.97", "12.52"),
            mt4_order(76915294, 1782197978000, 1782208753000, "4113.23", "4124.97", "11.74"),
            mt4_order(76920625, 1782201758000, 1782208754000, "4098.13", "4124.94", "26.81"),
        ],
        [
            binance_trade(7458901301, "BUY", "4", "4112.07", "2.29000000", 1782204123000),
            binance_trade(7458903116, "SELL", "4", "4112.25", "0", 1782204127000),
            binance_trade(7462648650, "BUY", "4", "4120.48", "-32.92000000", 1782207309000),
        ],
        [{"income": "0.80985952", "time": 1782201600000}],
        [
            {
                "id": 1,
                "ts": "2026-06-23T09:35:09+00:00",
                "kind": "exit_order",
                "payload": {"order_id": "7462648650", "timestamp_ms": 1782207309643},
            },
            {
                "id": 2,
                "ts": "2026-06-23T09:35:10+00:00",
                "kind": "open_pair_live_mismatch_paused",
                "payload": {
                    "pair_id": "pair_fa54010fc13a4ea0a29c68de",
                    "binance_position_qty": "0",
                    "mt4_positions": [
                        {"ticket": 76920625, "symbol": "XAUUSD", "side": "BUY", "lots": "0.01"},
                        {"ticket": 76915294, "symbol": "XAUUSD", "side": "BUY", "lots": "0.01"},
                        {"ticket": 76914311, "symbol": "XAUUSD", "side": "BUY", "lots": "0.01"},
                        {"ticket": 76914108, "symbol": "XAUUSD", "side": "BUY", "lots": "0.01"},
                    ],
                },
            },
        ],
    )

    assert len(items) == 1
    assert items[0].binance_exit_order_id == "7458901301 / 7462648650"
    assert items[0].binance_realized_pnl == Decimal("-30.63000000")
    assert items[0].mt4_profit == Decimal("58.80")
    assert items[0].binance_funding_income == Decimal("0.80985952")
    assert items[0].net_pnl == Decimal("28.97985952")
    assert "事件链" in items[0].status
    assert "原因：本单盈利+28.97985952U" in items[0].status
    assert "MT4盈亏+58.8U" in items[0].status
    assert "币安合约盈亏-30.63U" in items[0].status


def test_trade_history_event_links_prevent_manual_close_batch_cross_pair_mix():
    items = _build_trade_history(
        [
            mt4_order(1, 100_000, 200_100, "4081", "4017", "-64"),
            mt4_order(2, 110_000, 360_000, "4060", "4014", "-46"),
            mt4_order(3, 120_000, 360_001, "4044", "4014", "-30"),
            mt4_order(4, 190_000, 360_002, "4010", "4014", "4"),
        ],
        [
            binance_trade(800, "SELL", "1", "4083", "0", 100_000),
            binance_trade(801, "SELL", "1", "4063", "0", 110_000),
            binance_trade(802, "SELL", "1", "4047", "0", 120_000),
            binance_trade(900, "BUY", "3", "4022", "120", 200_000),
            binance_trade(803, "SELL", "1", "4014", "0", 210_000),
            binance_trade(901, "BUY", "1", "4016", "-2", 300_000),
        ],
        [],
        [
            {
                "id": 1,
                "ts": "1970-01-01T00:01:40+00:00",
                "kind": "v2_pair_open",
                "payload": {"pair_id": "old", "mt4_ticket": 1, "mt4_tickets": [1], "opened_ms": 100_000},
            },
            {
                "id": 2,
                "ts": "1970-01-01T00:02:00+00:00",
                "kind": "v2_pair_added",
                "payload": {"pair_id": "old", "mt4_tickets": [1, 2, 3], "opened_ms": 100_000},
            },
            {
                "id": 3,
                "ts": "1970-01-01T00:03:20+00:00",
                "kind": "v2_exit_order",
                "payload": {"order_id": "900", "timestamp_ms": 200_000},
            },
            {
                "id": 4,
                "ts": "1970-01-01T00:03:21+00:00",
                "kind": "v2_pair_closed",
                "payload": {"pair_id": "old", "tickets": [1, 2, 3]},
            },
            {
                "id": 5,
                "ts": "1970-01-01T00:03:30+00:00",
                "kind": "v2_pair_open",
                "payload": {"pair_id": "new", "mt4_ticket": 4, "mt4_tickets": [4], "opened_ms": 210_000},
            },
            {
                "id": 6,
                "ts": "1970-01-01T00:05:00+00:00",
                "kind": "v2_exit_order",
                "payload": {"order_id": "901", "timestamp_ms": 300_000},
            },
            {
                "id": 7,
                "ts": "1970-01-01T00:06:00+00:00",
                "kind": "manual_flat_pair_cleared",
                "payload": {"pair_id": "new", "binance_position_qty": "0", "mt4_positions": []},
            },
        ],
    )

    old_item = next(item for item in items if set(item.mt4_tickets or []) == {1, 2, 3})
    new_item = next(item for item in items if set(item.mt4_tickets or []) == {4})

    assert old_item.binance_exit_order_id == "900"
    assert old_item.binance_realized_pnl == Decimal("120")
    assert old_item.net_pnl == Decimal("-20")
    assert new_item.binance_exit_order_id == "901"
    assert new_item.net_pnl == Decimal("2")
    assert all(set(item.mt4_tickets or []) != {2, 3, 4} for item in items)


def test_trade_history_separates_v1_and_v2_versions(monkeypatch):
    monkeypatch.setattr(main_module.settings, "gold_v2_history_start_ms", 1500)

    items = main_module._build_trade_history(
        [
            mt4_order(1, 1000, 1100, "4100", "4099", "-1"),
            mt4_order(2, 2000, 2100, "4100", "4101", "1"),
        ],
        [],
        [],
    )

    assert len(items) == 2
    assert [item.strategy_version for item in items] == ["v2.0", "v1.0"]


class FakeHistoryClient:
    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = []

    async def user_trades(self, start_ms: int, end_ms: int, limit: int = 1000):
        self.calls.append((start_ms, end_ms, limit))
        if self.batches:
            return self.batches.pop(0)
        return [{"time": start_ms, "orderId": len(self.calls)}]


@pytest.mark.asyncio
async def test_binance_history_fetch_splits_windows_under_exchange_limit():
    client = FakeHistoryClient([])

    rows = await _fetch_binance_history_rows(
        client,
        "user_trades",
        0,
        30 * 86_400_000,
        limit=1000,
    )

    assert len(rows) == len(client.calls)
    assert len(client.calls) == 5
    assert all(end - start <= BINANCE_HISTORY_MAX_WINDOW_MS for start, end, _ in client.calls)


@pytest.mark.asyncio
async def test_binance_history_fetch_paginates_full_windows_by_last_time():
    client = FakeHistoryClient(
        [
            [{"time": 1000, "orderId": 1}, {"time": 1200, "orderId": 2}],
            [{"time": 1300, "orderId": 3}],
        ]
    )

    rows = await _fetch_binance_history_rows(client, "user_trades", 0, 10_000, limit=2)

    assert [row["orderId"] for row in rows] == [1, 2, 3]
    assert client.calls == [(0, 10_000, 2), (1201, 10_000, 2)]
