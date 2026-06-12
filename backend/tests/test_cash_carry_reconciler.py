from datetime import datetime, timezone
from decimal import Decimal

from app.core.models import ExchangeName
from app.services.cash_carry_execution_models import CashCarryPosition
from app.services.cash_carry_reconciler import _forced_close, build_cash_carry_external_perp_close_history


def test_build_external_perp_close_history_marks_liquidation() -> None:
    opened_at = datetime(2026, 6, 11, 4, 18, 43, tzinfo=timezone.utc)
    record = CashCarryPosition(
        id="pos-1",
        exchange=ExchangeName.BITGET,
        symbol="SKYAIUSDT",
        base_asset="SKYAI",
        quantity=Decimal("10"),
        spot_entry_price=Decimal("2"),
        perp_entry_price=Decimal("2"),
        spot_order_id="spot-open",
        perp_order_id="perp-open",
        opened_at=opened_at,
    )
    history = build_cash_carry_external_perp_close_history(
        _FakeSpot(),
        _FakeSwap(),
        record,
        "SKYAI/USDT",
        "SKYAI/USDT:USDT",
    )

    assert history["external_close_type"] == "liquidation"
    assert history["long_close_price"] is None
    assert history["short_close_price"] == "2.5"
    assert history["short_pnl"] == "-5.0"
    assert history["actual_net_profit"] == "-5.3"
    assert history["reconcile_status"] == "verified"


def test_gate_liq_text_is_treated_as_liquidation() -> None:
    trades = [{"info": {"text": "liq-6628823"}}]

    assert _forced_close(trades)


class _FakeSpot:
    def fetch_my_trades(self, _symbol, since=None, limit=100):
        return [
            {
                "order": "spot-open",
                "amount": "10",
                "cost": "20",
                "fee": {"currency": "USDT", "cost": "0.1"},
                "timestamp": 1781151523210,
            }
        ]


class _FakeSwap:
    has = {}

    def fetch_my_trades(self, _symbol, since=None, limit=100):
        return [
            {
                "order": "perp-open",
                "side": "sell",
                "amount": "10",
                "cost": "20",
                "fee": {"currency": "USDT", "cost": "0.1"},
                "timestamp": 1781151523551,
            },
            {
                "order": "force-close",
                "side": "buy",
                "amount": "10",
                "cost": "25",
                "fee": {"currency": "USDT", "cost": "0.1"},
                "timestamp": 1781157992290,
                "info": {"tradeSide": "burst_buy_single"},
            },
        ]

    def load_markets(self):
        return None

    def market(self, _symbol):
        return {"contractSize": "1"}
