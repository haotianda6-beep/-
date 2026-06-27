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
                            "entry_estimated_net_profit": "3.0",
                            "actual_vs_entry_estimate": "0.9",
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
    assert "开仓预估净利 3.0 USDT，真实偏差 0.9 USDT" in (rows[0].close_reason or "")


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
    assert rows[0].close_reason.startswith("合约腿已平，现货单腿已人工卖出；原因总结：")
    assert "真实净利" in rows[0].close_reason


def test_trade_history_store_shows_cash_carry_liquidation_mismatch(tmp_path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "cash_carry_execution_state.json").write_text(
        json.dumps(
            {
                "positions": [
                    {
                        "id": "cash-liquidated",
                        "exchange": "BITGET",
                        "symbol": "SKYAIUSDT",
                        "base_asset": "SKYAI",
                        "quantity": "1633",
                        "spot_entry_price": "0.1832",
                        "perp_entry_price": "0.1849",
                        "opened_at": "2026-06-11T04:18:43+00:00",
                        "close_reason": "BITGET SKYAIUSDT 合约腿已被交易所强平，现货仍持有，已标记 mismatch",
                        "status": "mismatch",
                        "history": {
                            "closed_at": "2026-06-11T06:06:32+00:00",
                            "quantity": "1633",
                            "long_open_price": "0.1832",
                            "long_close_price": None,
                            "short_open_price": "0.1849",
                            "short_close_price": "0.20326",
                            "actual_fee": "0.4",
                            "total_pnl": "-29.994",
                            "long_pnl": "0",
                            "short_pnl": "-29.994",
                            "funding_net": "0",
                            "actual_net_profit": "-30.394",
                            "long_order_ids": ["spot-open"],
                            "short_order_ids": ["perp-open", "force-close"],
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
    assert rows[0].symbol == "SKYAIUSDT"
    assert rows[0].closed_at.isoformat() == "2026-06-11T06:06:32+00:00"
    assert rows[0].long_close_price is None
    assert rows[0].actual_net_profit == Decimal("-30.394")
    assert rows[0].reconcile_status == "verified"
    assert "合约腿发生交易所强平" in rows[0].close_reason
