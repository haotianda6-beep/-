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
args = SimpleNamespace(quantity_tolerance_oz="0.01", loss_warning_ratio=0.70, profit_window_min_usdt="0.50")


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


def test_risk_warning_detects_near_max_loss():
    status = {
        "position_metrics": {
            "estimated_close_net": "-3.6",
            "max_pair_loss_usdt": "5",
        }
    }

    assert "接近最大亏损" in monitor.risk_warning_reason(status, args)


def test_risk_warning_ignores_small_drawdown():
    status = {
        "position_metrics": {
            "estimated_close_net": "-2.0",
            "max_pair_loss_usdt": "5",
        }
    }

    assert monitor.risk_warning_reason(status, args) is None


def test_profit_window_detects_positive_net_blocked_by_zero_target():
    status = {
        "state": "PAIR_OPEN",
        "execution_plan": {"active_binance_order": False},
        "gold_v2": {
            "exit_plan": {
                "target_exit_spread": "0",
                "current_exit_spread": "1.8",
            }
        },
        "position_metrics": {
            "estimated_close_net": "0.80",
            "exit_follow_buffer_usd_per_oz": "5.00",
        },
    }

    assert "预估净值 0.80U 已为正" in monitor.profit_window_reason(status, args)


def test_profit_window_ignores_active_order():
    status = {
        "state": "PAIR_OPEN",
        "execution_plan": {"active_binance_order": True},
        "gold_v2": {"exit_plan": {"target_exit_spread": "0", "current_exit_spread": "1.8"}},
        "position_metrics": {"estimated_close_net": "0.80"},
    }

    assert monitor.profit_window_reason(status, args) is None


def test_summarize_status_includes_add_plan_fields():
    status = {
        "state": "PAIR_OPEN",
        "binance_position_qty": "-1.000",
        "mt4_positions": [{"side": "BUY", "lots": "0.01"}],
        "gold_v2": {
            "selected_entry": {"ready": False},
            "add_plan": {
                "ready": False,
                "reason": "补仓后仍不安全",
                "current_edge": "3.1",
                "next_trigger_edge": "3.42",
                "add_count": 0,
                "exit_viable": False,
            },
            "exit_plan": {"target_exit_spread": "0"},
        },
    }

    summary = monitor.summarize_status(status)

    assert summary["add_reason"] == "补仓后仍不安全"
    assert summary["add_current_edge"] == "3.1"
    assert summary["add_next_trigger"] == "3.42"


def test_write_log_rotates_when_size_limit_is_reached(tmp_path):
    log_path = tmp_path / "monitor.log"
    log_path.write_text("x" * 128, encoding="utf-8")
    monitor.configure_log_rotation(max_log_mb=0.00001, backups=2)

    monitor.write_log(log_path, {"type": "tick"})

    assert log_path.exists()
    assert (tmp_path / "monitor.log.1").exists()
    assert '"type": "tick"' in log_path.read_text(encoding="utf-8")


def test_write_log_respects_backup_count(tmp_path):
    log_path = tmp_path / "monitor.log"
    log_path.write_text("new", encoding="utf-8")
    (tmp_path / "monitor.log.1").write_text("old1", encoding="utf-8")
    (tmp_path / "monitor.log.2").write_text("old2", encoding="utf-8")
    monitor.configure_log_rotation(max_log_mb=0.000001, backups=2)

    monitor.write_log(log_path, {"type": "tick"})

    assert (tmp_path / "monitor.log.1").read_text(encoding="utf-8") == "new"
    assert (tmp_path / "monitor.log.2").read_text(encoding="utf-8") == "old1"


def test_write_log_can_disable_stdout(tmp_path, capsys):
    log_path = tmp_path / "monitor.log"
    monitor.configure_log_rotation(max_log_mb=0, backups=3)

    monitor.configure_stdout_logging(True)
    monitor.write_log(log_path, {"type": "tick"})
    assert '"type": "tick"' in capsys.readouterr().out

    monitor.configure_stdout_logging(False)
    monitor.write_log(log_path, {"type": "tick2"})
    assert capsys.readouterr().out == ""
    assert '"type": "tick2"' in log_path.read_text(encoding="utf-8")

    monitor.configure_stdout_logging(True)


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
