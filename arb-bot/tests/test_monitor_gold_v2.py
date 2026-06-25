from pathlib import Path
from types import SimpleNamespace
import importlib.util
import sys


def load_monitor_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "monitor_gold_v2.py"
    spec = importlib.util.spec_from_file_location("monitor_gold_v2", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


monitor = load_monitor_module()
args = SimpleNamespace(quantity_tolerance_oz="0.01")


def test_exposure_issue_accepts_matched_short_pair():
    status = {
        "binance_position_qty": "-1.000",
        "open_pair": {"direction": "BINANCE_SHORT_MT4_LONG"},
        "mt4_positions": [{"side": "BUY", "lots": "0.01"}],
    }

    assert monitor.exposure_issue_reason(status, args) is None


def test_exposure_issue_detects_single_leg():
    status = {
        "binance_position_qty": "-1.000",
        "open_pair": {"direction": "BINANCE_SHORT_MT4_LONG"},
        "mt4_positions": [],
    }

    assert "单腿持仓" in monitor.exposure_issue_reason(status, args)


def test_exposure_issue_detects_quantity_mismatch():
    status = {
        "binance_position_qty": "-2.000",
        "open_pair": {"direction": "BINANCE_SHORT_MT4_LONG"},
        "mt4_positions": [{"side": "BUY", "lots": "0.01"}],
    }

    assert "数量不一致" in monitor.exposure_issue_reason(status, args)


def test_exposure_issue_detects_binance_direction_mismatch():
    status = {
        "binance_position_qty": "1.000",
        "open_pair": {"direction": "BINANCE_SHORT_MT4_LONG"},
        "mt4_positions": [{"side": "BUY", "lots": "0.01"}],
    }

    assert "币安应为空单" in monitor.exposure_issue_reason(status, args)


def test_exposure_issue_detects_mt4_direction_mismatch():
    status = {
        "binance_position_qty": "-1.000",
        "open_pair": {"direction": "BINANCE_SHORT_MT4_LONG"},
        "mt4_positions": [{"side": "SELL", "lots": "0.01"}],
    }

    assert "MT4 应为 BUY" in monitor.exposure_issue_reason(status, args)


def test_monitor_state_persists_cycle_progress(tmp_path):
    state_path = tmp_path / "monitor_state.json"
    state = monitor.MonitorState(
        start_event_id=100,
        opened_pairs={"pair_a"},
        closed_pairs={"pair_a"},
        target_reached=True,
        alerted_keys={"open:pair_a"},
    )

    monitor.save_monitor_state(state_path, state)
    restored = monitor.load_monitor_state(state_path, tmp_path / "missing.sqlite3")

    assert restored.start_event_id == 100
    assert restored.opened_pairs == {"pair_a"}
    assert restored.closed_pairs == {"pair_a"}
    assert restored.target_reached is True
    assert restored.alerted_keys == {"open:pair_a"}


def test_monitor_state_starts_from_current_event_when_no_state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "max_event_id", lambda path: 123)

    restored = monitor.load_monitor_state(tmp_path / "missing.json", tmp_path / "db.sqlite3")

    assert restored.start_event_id == 123
    assert restored.opened_pairs == set()
    assert restored.closed_pairs == set()


def test_alert_config_is_disabled_by_default(monkeypatch):
    for key in [
        "GOLD_ALERT_EMAIL_ENABLED",
        "GOLD_ALERT_EMAIL_TO",
        "GOLD_ALERT_SMTP_HOST",
    ]:
        monkeypatch.delenv(key, raising=False)

    config = monitor.load_alert_config()

    assert config.enabled is False
    assert config.ready is False


def test_send_alert_once_deduplicates(tmp_path, monkeypatch):
    sent = []
    config = monitor.AlertConfig(
        enabled=True,
        host="smtp.example.test",
        port=587,
        username="user",
        password="secret",
        recipients=("alert@example.test",),
        sender="bot@example.test",
        use_tls=True,
        use_ssl=False,
        timeout=1.0,
    )
    state = monitor.MonitorState(start_event_id=0, opened_pairs=set(), closed_pairs=set())
    monkeypatch.setattr(monitor, "send_email", lambda cfg, subject, body: sent.append((subject, body)))

    monitor.send_alert_once(config, state, "open:pair_a", "开仓", "body", tmp_path / "monitor.log")
    monitor.send_alert_once(config, state, "open:pair_a", "开仓", "body", tmp_path / "monitor.log")

    assert sent == [("开仓", "body")]
    assert state.alerted_keys == {"open:pair_a"}
