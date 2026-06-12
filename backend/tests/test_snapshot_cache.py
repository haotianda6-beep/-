import time

from app.api import routes
from app.core.models import BotSettings
from app.services.arbitrage_engine import ArbitrageEngine
from app.services.settings_store import SettingsStore


def test_snapshot_cache_returns_loading_snapshot_without_waiting(monkeypatch) -> None:
    routes._snapshot_cache = None
    routes._snapshot_json_cache = ""
    routes._snapshot_cache_at = 0.0
    routes._snapshot_refreshing = False
    monkeypatch.setattr(routes, "_lightweight_dashboard_enabled", lambda: False)

    def slow_snapshot():
        time.sleep(0.2)
        raise RuntimeError("slow exchange")

    monkeypatch.setattr(routes.engine, "snapshot", slow_snapshot)

    started = time.monotonic()
    snapshot = routes.snapshot_cached()

    assert time.monotonic() - started < 0.1
    assert snapshot.risk_events[0].title == "后台数据加载中"


def test_snapshot_cache_uses_lightweight_mode_by_default(monkeypatch) -> None:
    routes._snapshot_cache = None
    routes._snapshot_json_cache = ""
    routes._snapshot_cache_at = 0.0
    routes._snapshot_refreshing = False

    def fail_snapshot():
        raise AssertionError("heavy snapshot should not run")

    monkeypatch.setattr(routes.engine, "snapshot", fail_snapshot)

    snapshot = routes.snapshot_cached()

    assert snapshot.risk_events[0].title == "主控台轻量实时模式"
    assert snapshot.cash_carry_opportunities == []
    assert snapshot.trades == []


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
