import json
from decimal import Decimal
from pathlib import Path

from app.services.trade_history_store import TradeHistoryStore


def test_trade_history_store_reads_verified_cash_carry_history(tmp_path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "cash_carry_execution_state.json").write_text(
        json.dumps(
            {
                "positions": [
                    {
                        "id": "trade-1",
                        "exchange": "GATE",
                        "symbol": "SPCXUSDT",
                        "base_asset": "SPCX",
                        "quantity": "1",
                        "spot_entry_price": "100",
                        "perp_entry_price": "103",
                        "opened_at": "2026-06-09T00:00:00+00:00",
                        "closed_at": "2026-06-09T01:00:00+00:00",
                        "close_reason": "固定U止盈",
                        "status": "closed",
                        "history": {
                            "quantity": "1",
                            "long_close_price": "101",
                            "short_close_price": "100",
                            "actual_fee": "0.2",
                            "long_pnl": "1",
                            "short_pnl": "3",
                            "funding_net": "0.1",
                            "actual_net_profit": "3.9",
                            "long_order_ids": ["spot-open", "spot-close"],
                            "short_order_ids": ["swap-open", "swap-close"],
                            "reconcile_status": "verified",
                        },
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    rows = TradeHistoryStore(Path(tmp_path)).load()

    assert len(rows) == 1
    assert rows[0].strategy_type == "cash_carry"
    assert rows[0].actual_net_profit == Decimal("3.9")
    assert rows[0].reconcile_status == "verified"


def test_trade_history_store_reads_cross_and_reverse_closed_rows(tmp_path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "cross_spread_execution_state.json").write_text(
        json.dumps(
            {
                "positions": [
                    {
                        "id": "cross-1",
                        "symbol": "ABCUSDT",
                        "long_exchange": "BINANCE",
                        "short_exchange": "OKX",
                        "quantity": "1",
                        "long_entry_price": "100",
                        "short_entry_price": "103",
                        "opened_at": "2026-06-09T00:00:00+00:00",
                        "closed_at": "2026-06-09T01:00:00+00:00",
                        "close_reason": "价差收敛",
                        "status": "closed",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (config / "reverse_execution_state.json").write_text(
        json.dumps(
            {
                "positions": [
                    {
                        "id": "reverse-1",
                        "exchange": "BYBIT",
                        "symbol": "XYZUSDT",
                        "base_asset": "XYZ",
                        "quantity": "2",
                        "borrowed_quantity": "2",
                        "spot_entry_price": "10",
                        "perp_entry_price": "9.8",
                        "opened_at": "2026-06-09T02:00:00+00:00",
                        "closed_at": "2026-06-09T03:00:00+00:00",
                        "status": "closed",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    rows = TradeHistoryStore(Path(tmp_path)).load()
    strategies = {row.strategy_type for row in rows}

    assert strategies == {"perp_spread", "reverse_cash_carry"}
    assert all(row.reconcile_status == "pending" for row in rows)


def test_trade_history_store_keeps_unreconciled_cash_carry_closed_rows(tmp_path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "cash_carry_execution_state.json").write_text(
        json.dumps(
            {
                "positions": [
                    {
                        "id": "cash-raw",
                        "exchange": "GATE",
                        "symbol": "AIAUSDT",
                        "base_asset": "AIA",
                        "quantity": "1845",
                        "spot_entry_price": "0.0542",
                        "perp_entry_price": "0.0546",
                        "spot_close_price": "0.05375823",
                        "perp_close_price": "0.05417",
                        "opened_at": "2026-06-09T02:00:00+00:00",
                        "closed_at": "2026-06-09T03:00:00+00:00",
                        "close_reason": "manual_flattened_single_leg_after_perp_closed",
                        "status": "closed",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    rows = TradeHistoryStore(Path(tmp_path)).load()

    assert len(rows) == 1
    assert rows[0].strategy_type == "cash_carry"
    assert rows[0].symbol == "AIAUSDT"
    assert rows[0].reconcile_status == "pending"
    assert rows[0].close_reason == "合约腿已平，现货单腿已人工卖出"
