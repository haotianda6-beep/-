import time
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.api import routes
from app.core.models import BotSettings, CashCarryOpportunity, DataSource, ExchangeName
from app.services.arbitrage_engine import ArbitrageEngine
from app.services.live_market_types import CashCarryScan
from app.services.settings_store import SettingsStore


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


def test_cash_positions_snapshot_refreshes_in_background(tmp_path) -> None:
    engine = ArbitrageEngine(SettingsStore(tmp_path / "settings.json"))
    engine.cash_carry_positions = _SlowCashPositionBuilder()

    started = time.monotonic()
    rows = engine._cash_positions_snapshot([], [], BotSettings())

    assert rows == []
    assert time.monotonic() - started < 0.1
    assert engine._cash_positions_refreshing is True
    assert engine.cash_carry_positions.started is True


class _SlowCashPositionBuilder:
    started = False

    def has_open_state_records(self):
        return True

    def build(self, positions, cash_prices, settings):
        self.started = True
        time.sleep(0.2)
        return []


def _cash_opportunity() -> CashCarryOpportunity:
    return CashCarryOpportunity(
        exchange=ExchangeName.BINANCE,
        symbol="XAUUSDT",
        spot_price=Decimal("4200"),
        perp_price=Decimal("4236"),
        basis_pct=Decimal("0.8571"),
        funding_rate_pct=Decimal("0.01"),
        quantity=Decimal("0.01"),
        spot_volume_24h_usdt=Decimal("1000000"),
        perp_volume_24h_usdt=Decimal("1000000"),
        estimated_basis_profit=Decimal("0.85"),
        estimated_funding_income=Decimal("0.01"),
        estimated_open_close_fee=Decimal("0.08"),
        estimated_net_profit=Decimal("0.78"),
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
