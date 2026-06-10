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
