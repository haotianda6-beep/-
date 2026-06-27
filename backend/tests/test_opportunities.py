from decimal import Decimal
from datetime import datetime, timedelta, timezone

from app.core.models import BotSettings, CashCarryPositionRow, ExchangeName, PositionSnapshot
from app.services.arbitrage_engine import ArbitrageEngine
from app.services.cash_carry_history_quality import CashCarryHistoryQuality
from app.services.cash_carry_state import CashCarryStateStore
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


def test_cash_carry_add_config_warns_when_limits_block_first_add(tmp_path) -> None:
    engine = ArbitrageEngine(SettingsStore(tmp_path / "settings.json"))
    settings = BotSettings(
        cash_carry_enabled=True,
        cash_carry_auto_open_enabled=True,
        order_notional_usdt=Decimal("500"),
        max_symbol_notional_usdt=Decimal("500"),
        single_exchange_max_notional_usdt=Decimal("1000"),
        max_total_notional_usdt=Decimal("2000"),
        max_add_count=4,
        add_trigger_spread_pct=Decimal("2"),
    )

    events = engine.get_risk_events(settings)

    event = next(item for item in events if item.id == "cash-carry-add-config-blocked")
    assert event.title == "正向期现补仓参数不可执行"
    assert "单币最大仓位 500U" in event.detail


def test_cash_carry_liquidation_distance_warns_when_too_close(tmp_path) -> None:
    engine = ArbitrageEngine(SettingsStore(tmp_path / "settings.json"))

    events = engine.get_risk_events(
        BotSettings(),
        live_positions=[
            PositionSnapshot(
                exchange=ExchangeName.BITGET,
                symbol="ABCUSDT",
                side="short",
                quantity=Decimal("100"),
                entry_price=Decimal("1"),
                mark_price=Decimal("1"),
                leverage=Decimal("3"),
                unrealized_pnl=Decimal("-1"),
                liquidation_price=Decimal("1.08"),
            )
        ],
    )

    event = next(item for item in events if item.id == "liq-distance-BITGET-ABCUSDT")
    assert event.title == "正向期现强平距离过近"
    assert event.severity == "critical"


def test_cash_carry_v2_performance_event_reports_win_rate(tmp_path) -> None:
    state = tmp_path / "cash_carry_execution_state.json"
    state.write_text(
        '{"positions":['
        '{"exchange":"BITGET","symbol":"AAAUSDT","status":"closed","closed_at":"2026-06-27T01:00:00+00:00","history":{"actual_net_profit":"1.2"}},'
        '{"exchange":"BITGET","symbol":"BBBUSDT","status":"closed","closed_at":"2026-06-27T02:00:00+00:00","history":{"actual_net_profit":"-0.4"}}'
        ']}',
        encoding="utf-8",
    )
    engine = ArbitrageEngine(SettingsStore(tmp_path / "settings.json"))
    engine.cash_carry_history_quality = CashCarryHistoryQuality(state)

    events = engine.get_risk_events(BotSettings(),)

    event = next(item for item in events if item.id == "cash-carry-v2-performance")
    assert event.title == "正向期现V2统计"
    assert "历史真实样本 2 单" in event.detail
    assert "胜率 50.00%" in event.detail


def test_cash_carry_turnover_event_warns_for_stale_unprofitable_position(tmp_path) -> None:
    opened_at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=7)
    state = tmp_path / "cash_carry_execution_state.json"
    state.write_text(
        '{"positions":[{"id":"pos-1","exchange":"GATE","symbol":"AAAUSDT","base_asset":"AAA","quantity":"1","spot_entry_price":"1","perp_entry_price":"1.02","opened_at":"'
        + opened_at.isoformat()
        + '","status":"open"}]}',
        encoding="utf-8",
    )
    engine = ArbitrageEngine(SettingsStore(tmp_path / "settings.json"))
    engine.cash_carry_state = CashCarryStateStore(state)
    row = _cash_position().model_copy(update={
        "symbol": "AAAUSDT",
        "current_net_profit": Decimal("-0.4"),
        "estimated_funding_rate_pct": Decimal("0"),
        "basis_pct": Decimal("0.6"),
    })

    events = engine.get_risk_events(BotSettings(order_notional_usdt=Decimal("100")), cash_carry_positions=[row])

    event = next(item for item in events if item.id == "cash-carry-turnover-GATE-AAAUSDT")
    assert event.title == "正向期现持仓周转过慢"
    assert "影响约10单/日目标" in event.detail


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
