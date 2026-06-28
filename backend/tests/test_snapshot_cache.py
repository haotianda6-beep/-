import time
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.api import routes
from app.core.models import BotSettings, CashCarryOpportunity, CashCarryPositionRow, DataSource, ExchangeName, PositionSnapshot
from app.services.arbitrage_engine import ArbitrageEngine
from app.services.cash_carry_market_memory import CashCarryMarketMemory
from app.services.live_market_types import CashCarryScan
from app.services.settings_store import SettingsStore


@pytest.fixture(autouse=True)
def isolate_route_engine_cash_carry_memory(monkeypatch, tmp_path) -> None:
    memory = CashCarryMarketMemory(tmp_path / "cash_carry_execution_state.json")
    monkeypatch.setattr(routes.engine, "cash_carry_market_memory", memory)


def test_snapshot_cache_returns_loading_snapshot_without_waiting(monkeypatch) -> None:
    _reset_snapshot_state()
    monkeypatch.setattr(routes, "_lightweight_dashboard_enabled", lambda: False)

    def slow_snapshot():
        time.sleep(0.2)
        raise RuntimeError("slow exchange")

    monkeypatch.setattr(routes.engine, "snapshot", slow_snapshot)

    started = time.monotonic()
    snapshot = routes.snapshot_cached()

    assert time.monotonic() - started < 0.15
    assert snapshot.risk_events[0].title == "后台数据加载中"
    _wait_until_not_refreshing()


def test_snapshot_cache_uses_lightweight_mode_by_default(monkeypatch) -> None:
    _reset_snapshot_state()
    monkeypatch.setattr(routes.engine.live_read, "live_data_enabled", lambda: False)

    def fail_snapshot():
        raise AssertionError("heavy snapshot should not run")

    monkeypatch.setattr(routes.engine, "snapshot", fail_snapshot)

    snapshot = routes.snapshot_cached()

    assert snapshot.risk_events[0].title == "后台数据加载中"
    cached = _wait_for_snapshot_title("主控台轻量实时模式")
    assert cached.cash_carry_opportunities == []
    assert cached.trades == []


def test_lightweight_snapshot_returns_loading_without_waiting_for_runtime(monkeypatch) -> None:
    _reset_snapshot_state()
    monkeypatch.setattr(routes, "_lightweight_dashboard_enabled", lambda: True)

    def slow_lightweight_snapshot():
        time.sleep(0.2)
        raise RuntimeError("slow runtime")

    monkeypatch.setattr(routes, "_lightweight_snapshot", slow_lightweight_snapshot)

    started = time.monotonic()
    snapshot = routes.snapshot_cached()

    assert time.monotonic() - started < 0.15
    assert snapshot.risk_events[0].title == "后台数据加载中"
    _wait_until_not_refreshing()


def test_lightweight_snapshot_keeps_cash_carry_realtime_rows(monkeypatch) -> None:
    _reset_snapshot_state()
    opportunity = _cash_opportunity()
    runtime = SimpleNamespace(
        account=SimpleNamespace(balances=[], positions=[], issues=[]),
        cash_carry=CashCarryScan(opportunities=[opportunity], candidates=[opportunity], issues=[]),
    )
    monkeypatch.setattr(routes.engine.live_read, "live_data_enabled", lambda: True)
    monkeypatch.setattr(routes.engine.live_runtime, "get", lambda _settings: runtime)

    routes.snapshot_cached()
    snapshot = _wait_for_snapshot_title("主控台轻量实时模式")

    assert snapshot.cash_carry_opportunities == [opportunity]
    assert snapshot.cash_carry_candidates == [opportunity]
    assert snapshot.trades == []
    assert snapshot.mt4_spread_opportunities == []


def test_lightweight_snapshot_trims_display_candidates_but_diagnoses_raw_pool(monkeypatch) -> None:
    _reset_snapshot_state()
    base = _cash_opportunity().model_copy(update={"blocked_reasons": ["合约溢价未达 0.8%"]})
    candidates = [base.model_copy(update={"symbol": f"ABC{i}USDT"}) for i in range(80)]
    runtime = SimpleNamespace(
        account=SimpleNamespace(balances=[], positions=[], issues=[]),
        cash_carry=CashCarryScan(opportunities=[], candidates=candidates, issues=[]),
        alpha_alert=SimpleNamespace(opportunities=[], candidates=[], issues=[]),
    )
    monkeypatch.setattr(routes.engine.live_read, "live_data_enabled", lambda: True)
    monkeypatch.setattr(routes.engine.live_runtime, "get", lambda _settings: runtime)

    routes.snapshot_cached()
    snapshot = _wait_for_snapshot_title("主控台轻量实时模式")

    assert len(snapshot.cash_carry_candidates) == 50
    event = next(item for item in snapshot.risk_events if item.id == "cash-carry-frequency-diagnostic")
    assert "当前候选 80 个" in event.detail


def test_cash_positions_snapshot_refreshes_in_background(tmp_path) -> None:
    engine = ArbitrageEngine(SettingsStore(tmp_path / "settings.json"))
    engine.cash_carry_positions = _SlowCashPositionBuilder()

    started = time.monotonic()
    rows = engine._cash_positions_snapshot([], [], BotSettings())

    assert rows == []
    assert time.monotonic() - started < 0.1
    assert engine._cash_positions_refreshing is True
    assert engine.cash_carry_positions.started is True


def test_cash_positions_snapshot_drops_stale_spot_only_cache_when_live_perp_arrives(tmp_path) -> None:
    engine = ArbitrageEngine(SettingsStore(tmp_path / "settings.json"))
    engine.cash_carry_positions = _SlowCashPositionBuilder()
    engine._cash_positions_cache = [_cash_position_row(status="spot_only", perp_side="none", perp_base="0")]
    engine._cash_positions_cache_at = time.monotonic()

    rows = engine._cash_positions_snapshot([_live_position()], [], BotSettings())

    assert rows == []
    assert engine._cash_positions_refreshing is True


class _SlowCashPositionBuilder:
    started = False

    def has_open_state_records(self):
        return True

    def build(self, positions, cash_prices, settings):
        self.started = True
        time.sleep(0.2)
        return []


def _live_position() -> PositionSnapshot:
    return PositionSnapshot(
        exchange=ExchangeName.BITGET,
        symbol="USUSDT",
        side="short",
        quantity=Decimal("100"),
        entry_price=Decimal("1"),
        mark_price=Decimal("1.1"),
        leverage=Decimal("3"),
        unrealized_pnl=Decimal("-10"),
        liquidation_price=Decimal("1.8"),
    )


def _cash_position_row(status: str = "matched", perp_side: str = "short", perp_base: str = "100") -> CashCarryPositionRow:
    return CashCarryPositionRow(
        exchange=ExchangeName.BITGET,
        symbol="USUSDT",
        status=status,
        spot_quantity=Decimal("100"),
        spot_entry_price=Decimal("1"),
        spot_price=Decimal("1"),
        spot_unrealized_pnl=Decimal("0"),
        perp_side=perp_side,
        perp_contracts=Decimal("100"),
        perp_base_quantity=Decimal(perp_base),
        contract_size=Decimal("1"),
        perp_entry_price=Decimal("1"),
        perp_mark_price=Decimal("1.1"),
        leverage=Decimal("3"),
        perp_unrealized_pnl=Decimal("-10"),
        estimated_funding_rate_pct=Decimal("0"),
        estimated_funding_income=Decimal("0"),
        estimated_open_fee=Decimal("0.1"),
        estimated_close_fee=Decimal("0.1"),
        current_net_profit=Decimal("-10.2"),
        quantity_gap=Decimal("0"),
        basis_pct=Decimal("10"),
        updated_at=datetime.now(timezone.utc),
    )


def _cash_opportunity() -> CashCarryOpportunity:
    return CashCarryOpportunity(
        exchange=ExchangeName.GATE,
        symbol="ABCUSDT",
        spot_price=Decimal("100"),
        perp_price=Decimal("101.5"),
        basis_pct=Decimal("1.5000"),
        funding_rate_pct=Decimal("0.02"),
        quantity=Decimal("1"),
        spot_volume_24h_usdt=Decimal("1000000"),
        perp_volume_24h_usdt=Decimal("1000000"),
        estimated_basis_profit=Decimal("1.30"),
        estimated_funding_income=Decimal("0.02"),
        estimated_open_close_fee=Decimal("0.08"),
        estimated_net_profit=Decimal("1.24"),
        notional_usdt=Decimal("100"),
        margin_required_usdt=Decimal("50"),
        leverage=Decimal("2"),
        blocked_reasons=[],
        data_source=DataSource.LIVE,
        updated_at=datetime.now(timezone.utc),
    )


def _wait_for_snapshot_title(title: str):
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        snapshot = routes._snapshot_cache
        if snapshot and snapshot.risk_events and snapshot.risk_events[0].title == title:
            return snapshot
        time.sleep(0.02)
    raise AssertionError(f"snapshot title did not become {title!r}")


def _wait_until_not_refreshing() -> None:
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if not routes._snapshot_refreshing:
            return
        time.sleep(0.02)
    raise AssertionError("snapshot refresh thread did not finish")


def _reset_snapshot_state() -> None:
    _wait_until_not_refreshing()
    routes._snapshot_cache = None
    routes._snapshot_json_cache = ""
    routes._snapshot_cache_at = 0.0
    routes._snapshot_refreshing = False
    routes.engine._cash_positions_cache = []
    routes.engine._cash_positions_cache_at = 0.0
    routes.engine._cash_positions_refreshing = False
