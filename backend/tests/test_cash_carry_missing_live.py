import json

from app.core.models import BotSettings
from app.services.cash_carry_executor import CashCarryExecutor


def test_cash_carry_marks_open_state_mismatch_when_live_position_is_missing(tmp_path) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        '{"positions":[{"id":"pos-1","exchange":"GATE","symbol":"ABCUSDT","base_asset":"ABC","quantity":"1","spot_entry_price":"100","perp_entry_price":"101","spot_order_id":"s1","perp_order_id":"p1","opened_at":"2026-06-09T00:00:00+00:00","status":"open"}]}',
        encoding="utf-8",
    )
    executor = CashCarryExecutor(state)
    settings = BotSettings(manual_confirm_required=True, cash_carry_auto_close_enabled=True)

    other_live = type("Row", (), {"exchange": "GATE", "symbol": "OTHERUSDT"})()
    result = executor.evaluate_close([], settings, [other_live])

    assert result is not None
    assert result.status == "failed"
    assert json.loads(state.read_text(encoding="utf-8"))["positions"][0]["status"] == "mismatch"


def test_cash_carry_does_not_mark_mismatch_while_positions_are_loading(tmp_path) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        '{"positions":[{"id":"pos-1","exchange":"GATE","symbol":"ABCUSDT","base_asset":"ABC","quantity":"1","spot_entry_price":"100","perp_entry_price":"101","opened_at":"2026-06-09T00:00:00+00:00","status":"open"}]}',
        encoding="utf-8",
    )
    executor = CashCarryExecutor(state)

    assert executor.evaluate_close([], BotSettings(cash_carry_auto_close_enabled=True), []) is None
    assert json.loads(state.read_text(encoding="utf-8"))["positions"][0]["status"] == "open"


def test_cash_carry_auto_sells_orphan_spot_after_external_perp_close(tmp_path) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        '{"positions":[{"id":"pos-1","exchange":"BITGET","symbol":"ABCUSDT","base_asset":"ABC","quantity":"10","spot_entry_price":"2","perp_entry_price":"2.1","spot_order_id":"spot-open","perp_order_id":"perp-open","opened_at":"2026-06-09T00:00:00+00:00","status":"open"}]}',
        encoding="utf-8",
    )
    executor = _OrphanCloseExecutor(state)
    settings = BotSettings(manual_confirm_required=True, cash_carry_auto_close_enabled=True)
    other_live = type("Row", (), {"exchange": "BITGET", "symbol": "OTHERUSDT"})()

    result = executor.evaluate_close([], settings, [other_live])
    saved = json.loads(state.read_text(encoding="utf-8"))["positions"][0]

    assert result is not None
    assert result.status == "close_submitted"
    assert executor.spot.orders[0]["side"] == "sell"
    assert executor.spot.orders[0]["amount"] == 10.0
    assert saved["status"] == "closed"
    assert "自动卖出现货孤腿" in saved["close_reason"]
    assert saved["history"]["actual_net_profit"] == "-1"


class _OrphanCloseExecutor(CashCarryExecutor):
    def __init__(self, state_path):
        super().__init__(state_path)
        self.spot = _FakeSpot()
        self.swap = _FakeSwap()

    def _exchange(self, exchange_name, default_type):
        return self.spot if default_type == "spot" else self.swap

    def _missing_live_perp_status(self, record):
        return "BITGET ABCUSDT 合约腿已被交易所强平，现货仍持有，已标记 mismatch", {
            "history": {
                "close_perp_order_id": "perp-force-close",
                "short_close_price": "2.5",
                "external_close_type": "liquidation",
            }
        }

    def _orphan_close_history(self, spot, swap, record, spot_symbol, swap_symbol, close_spot_order_id, close_perp_order_id):
        return {
            "quantity": "10",
            "long_close_price": "2.4",
            "short_close_price": "2.5",
            "actual_net_profit": "-1",
            "reconcile_status": "verified",
        }

    def _safety_gate(self, settings, opening, protective=False):
        return []


class _FakeSpot:
    has = {"fetchOrder": False}

    def __init__(self):
        self.orders = []

    def fetch_balance(self, params):
        return {"ABC": {"free": "10"}}

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        order = {"id": "spot-close", "symbol": symbol, "side": side, "amount": amount, "average": 2.4, "cost": 24}
        self.orders.append(order)
        return order


class _FakeSwap:
    pass
