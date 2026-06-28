import json
from datetime import datetime, timezone
from decimal import Decimal

from app.core.models import BotSettings, CashCarryOpportunity, DataSource, ExchangeName
from app.services.cash_carry_executor import CashCarryExecutor
from app.services.cash_carry_state import CashCarryStateStore


def test_cash_carry_does_not_reopen_recently_closed_symbol(tmp_path) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps({"positions": [{"id": "old", "exchange": "GATE", "symbol": "AIAUSDT", "status": "closed", "closed_at": datetime.now(timezone.utc).isoformat()}]}),
        encoding="utf-8",
    )
    executor = CashCarryExecutor(state)
    settings = BotSettings(cash_carry_auto_open_enabled=True, manual_confirm_required=False)

    assert executor.evaluate_open([_opportunity()], settings) is None


def test_cash_carry_restores_mismatch_when_live_position_is_matched(tmp_path) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        '{"positions":[{"id":"pos-1","exchange":"GATE","symbol":"AIAUSDT","base_asset":"AIA","quantity":"1","spot_entry_price":"1","perp_entry_price":"1.1","opened_at":"2026-06-09T00:00:00+00:00","status":"mismatch","close_reason":"stale"}]}',
        encoding="utf-8",
    )
    executor = CashCarryExecutor(state)

    assert executor.evaluate_close([], BotSettings(cash_carry_auto_close_enabled=True), [_live_row()]) is None
    item = json.loads(state.read_text(encoding="utf-8"))["positions"][0]
    assert item["status"] == "open"
    assert "close_reason" not in item


def test_cash_carry_state_read_tolerates_empty_file(tmp_path) -> None:
    state = tmp_path / "state.json"
    state.write_text("", encoding="utf-8")

    assert CashCarryStateStore(state).read() == {"positions": []}


def test_cash_carry_state_extracts_recent_depth_basis_haircut(tmp_path) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        '{"positions":[],"recent_depth_blocks":['
        '{"exchange":"GATE","symbol":"ABCUSDT","reason":"深度均价开仓基差 0.1000% 低于 0.5000% ","basis_pct":"0.4000","at":"2026-06-28T00:00:00+00:00"},'
        '{"exchange":"BITGET","symbol":"XYZUSDT","reason":"深度均价开仓基差 0.2000% 低于 0.5000% ","basis_pct":"0.3000","at":"2026-06-28T00:00:00+00:00"}'
        ']}',
        encoding="utf-8",
    )
    now = datetime(2026, 6, 28, 0, 1, tzinfo=timezone.utc)

    store = CashCarryStateStore(state)
    assert store.recent_depth_basis_haircut_pct(ExchangeName.GATE, now=now) == Decimal("0.3000")
    assert store.recent_depth_basis_haircut_pct(ExchangeName.BITGET, now=now) == Decimal("0.1000")
    assert store.recent_depth_basis_haircut_pct(ExchangeName.GATE, symbol="ABCUSDT", now=now) == Decimal("0.3000")
    assert store.recent_depth_basis_haircut_pct(ExchangeName.GATE, symbol="OTHERUSDT", now=now) == Decimal("0.3000")
    assert store.recent_depth_basis_haircut_pct(
        ExchangeName.GATE,
        symbol="OTHERUSDT",
        now=now,
        exchange_fallback=False,
    ) == Decimal("0")


def _opportunity() -> CashCarryOpportunity:
    return CashCarryOpportunity(
        exchange=ExchangeName.GATE,
        symbol="AIAUSDT",
        spot_price=Decimal("1"),
        perp_price=Decimal("1.02"),
        basis_pct=Decimal("2"),
        funding_rate_pct=Decimal("0.01"),
        quantity=Decimal("1"),
        spot_volume_24h_usdt=Decimal("1000000"),
        perp_volume_24h_usdt=Decimal("1000000"),
        estimated_basis_profit=Decimal("2"),
        estimated_funding_income=Decimal("0.01"),
        estimated_open_close_fee=Decimal("0.2"),
        estimated_net_profit=Decimal("1.81"),
        blocked_reasons=[],
        data_source=DataSource.LIVE,
        updated_at=datetime.now(timezone.utc),
    )


def _live_row():
    return type(
        "Row",
        (),
        {"exchange": "GATE", "symbol": "AIAUSDT", "status": "matched", "basis_pct": Decimal("2"), "current_net_profit": Decimal("0"), "estimated_funding_rate_pct": Decimal("0.01")},
    )()
