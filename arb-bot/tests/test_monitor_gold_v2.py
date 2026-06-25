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
