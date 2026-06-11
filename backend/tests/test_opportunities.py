from decimal import Decimal
from datetime import datetime, timezone

from app.core.models import BotSettings, CashCarryPositionRow, ExchangeName
from app.services.arbitrage_engine import ArbitrageEngine
from app.services.market_checks import TransferNetworks, bidirectional_route_check
from app.services.settings_store import SettingsStore


def test_bidirectional_route_check_explains_missing_or_mismatched_networks() -> None:
    missing = bidirectional_route_check("ABC", {}, {}, "OKX", "BYBIT")
    assert not missing.ok
    assert "属于未确认" in missing.reasons[0]

    confirmed_absent = bidirectional_route_check("ABC", {}, {}, "OKX", "BYBIT", True, True)
    assert not confirmed_absent.ok
    assert "按当前接口确认" in confirmed_absent.reasons[0]

    mismatch = bidirectional_route_check(
        "ABC",
        {"ABC": TransferNetworks(deposit={"ETH"}, withdraw={"ETH"})},
        {"ABC": TransferNetworks(deposit={"TRX"}, withdraw={"TRX"})},
        "OKX",
        "BYBIT",
    )
    assert not mismatch.ok
    assert "已确认" in " / ".join(mismatch.reasons)


def test_matched_cash_carry_position_suppresses_stale_execution_warning(tmp_path, monkeypatch) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    engine = ArbitrageEngine(store)
    monkeypatch.setattr(
        "app.services.arbitrage_engine.recent_execution_results",
        lambda: [{"strategy_id": "cash-carry", "title": "正向期现执行器", "status": "blocked_by_safety_gate", "reason": "GATE SPCXUSDT 当前现货 1.3214559，合约折算 1.98，数量不一致"}],
    )

    events = engine.get_risk_events(BotSettings(), cash_carry_positions=[_cash_position()])

    assert all("数量不一致" not in item.detail for item in events)


def test_execution_rate_limit_reason_is_localized(tmp_path, monkeypatch) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    engine = ArbitrageEngine(store)
    monkeypatch.setattr(
        "app.services.arbitrage_engine.recent_execution_results",
        lambda: [{"strategy_id": "cash-carry", "title": "正向期现执行器", "status": "failed", "reason": 'bybit {"retCode":10006,"retMsg":"Too many visits. Exceeded the API Rate Limit."}', "at": datetime.now(timezone.utc).isoformat()}],
    )

    events = engine.get_risk_events(BotSettings())

    assert any(item.detail == "交易所 API 限频，系统已短暂冷却并等待自动重试。" for item in events)


def _cash_position() -> CashCarryPositionRow:
    now = datetime.now(timezone.utc)
    return CashCarryPositionRow(
        exchange=ExchangeName.GATE,
        symbol="SPCXUSDT",
        status="matched",
        spot_quantity=Decimal("1.977805"),
        spot_entry_price=Decimal("151"),
        spot_price=Decimal("152"),
        spot_unrealized_pnl=Decimal("1"),
        perp_side="short",
        perp_contracts=Decimal("198"),
        perp_base_quantity=Decimal("1.98"),
        contract_size=Decimal("0.01"),
        perp_entry_price=Decimal("157"),
        perp_mark_price=Decimal("152"),
        leverage=Decimal("2"),
        perp_unrealized_pnl=Decimal("1"),
        estimated_funding_rate_pct=Decimal("0.01"),
        estimated_funding_income=Decimal("0.03"),
        estimated_open_fee=Decimal("0.1"),
        estimated_close_fee=Decimal("0.1"),
        current_net_profit=Decimal("1.83"),
        quantity_gap=Decimal("-0.002195"),
        basis_pct=Decimal("0.1"),
        updated_at=now,
    )
