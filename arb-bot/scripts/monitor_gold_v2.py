#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import json
import smtplib
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from pathlib import Path
from typing import Any


LOG_MAX_BYTES = 0
LOG_BACKUPS = 3
STDOUT_LOGGING = True


@dataclass
class MonitorState:
    start_event_id: int
    opened_pairs: set[str]
    closed_pairs: set[str]
    exposure_issue_since: float | None = None
    exposure_issue_reason: str | None = None
    risk_issue_since: float | None = None
    risk_issue_reason: str | None = None
    profit_window_since: float | None = None
    profit_window_reason: str | None = None
    last_state: str | None = None
    target_reached: bool = False
    alerted_keys: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class AlertConfig:
    enabled: bool
    host: str
    port: int
    username: str
    password: str
    recipients: tuple[str, ...]
    sender: str
    use_tls: bool
    use_ssl: bool
    timeout: float

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.host) and bool(self.recipients)


def main() -> int:
    args = parse_args()
    load_env_file(Path(args.env_file) if args.env_file else None)
    alert_config = load_alert_config()
    log_path = Path(args.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    configure_log_rotation(args.max_log_mb, args.log_backups)
    configure_stdout_logging(not args.quiet)
    state_path = Path(args.state_file) if args.state_file else None
    state = load_monitor_state(state_path, Path(args.db))
    deadline = time.monotonic() + args.max_minutes * 60
    write_log(
        log_path,
        {
            "type": "monitor_start",
            "start_event_id": state.start_event_id,
            "cycles_target": args.cycles,
            "closed_cycles": len(state.closed_pairs),
            "state_file": str(state_path) if state_path else None,
            "email_alert_enabled": alert_config.ready,
            "email_alert_to": mask_recipients(alert_config.recipients),
        },
    )
    save_monitor_state(state_path, state)

    while time.monotonic() < deadline:
        now = time.monotonic()
        try:
            status = fetch_json(args.status_url, args.http_timeout)
            check_status(status, state, args, log_path, now, alert_config)
        except Exception as exc:
            write_log(log_path, {"type": "status_error", "error": short_error(exc)})
            send_alert_once(
                alert_config,
                state,
                "status_error",
                "黄金监控读取状态失败",
                f"黄金执行器状态读取失败：{short_error(exc)}",
                log_path,
            )

        events = read_events(Path(args.db), state.start_event_id)
        for event in events:
            handle_event(event, state, log_path, alert_config)
        if len(state.closed_pairs) >= args.cycles and not state.target_reached:
            state.target_reached = True
            write_log(log_path, {"type": "monitor_target_reached", "closed_cycles": len(state.closed_pairs)})
            send_alert_once(
                alert_config,
                state,
                "target_reached",
                "黄金监控已完成目标轮数",
                f"黄金 V2 监控已记录 {len(state.closed_pairs)} 轮平仓，达到目标 {args.cycles} 轮。",
                log_path,
            )
        save_monitor_state(state_path, state)
        time.sleep(args.interval)

    write_log(
        log_path,
        {
            "type": "monitor_timeout",
            "closed_cycles": len(state.closed_pairs),
            "opened_cycles": len(state.opened_pairs),
        },
    )
    return 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor live Gold V2 arbitrage cycles without trading.")
    parser.add_argument("--status-url", default="http://127.0.0.1:8011/status")
    parser.add_argument("--db", default="/root/perp-arb-bot/arb-bot/data/arb.sqlite3")
    parser.add_argument("--log-file", default="/root/perp-arb-bot/arb-bot/data/gold_v2_monitor.log")
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--max-minutes", type=float, default=360.0)
    parser.add_argument("--http-timeout", type=float, default=8.0)
    parser.add_argument("--single-leg-grace", type=float, default=20.0)
    parser.add_argument("--quantity-tolerance-oz", default="0.01")
    parser.add_argument("--loss-warning-ratio", type=float, default=0.70)
    parser.add_argument("--profit-window-min-usdt", default="0.50")
    parser.add_argument("--profit-window-grace", type=float, default=30.0)
    parser.add_argument("--max-log-mb", type=float, default=20.0)
    parser.add_argument("--log-backups", type=int, default=3)
    parser.add_argument("--state-file", default="/root/perp-arb-bot/arb-bot/data/gold_v2_monitor_state.json")
    parser.add_argument("--env-file", default="/root/perp-arb-bot/arb-bot/.env")
    parser.add_argument("--quiet", action="store_true", help="Write file logs only; do not print every tick to stdout.")
    return parser.parse_args()


def load_monitor_state(state_path: Path | None, db_path: Path) -> MonitorState:
    if state_path and state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            return MonitorState(
                start_event_id=int(data.get("start_event_id") or 0),
                opened_pairs=set(data.get("opened_pairs") or []),
                closed_pairs=set(data.get("closed_pairs") or []),
                target_reached=bool(data.get("target_reached")),
                alerted_keys=set(data.get("alerted_keys") or []),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    return MonitorState(start_event_id=max_event_id(db_path), opened_pairs=set(), closed_pairs=set())


def save_monitor_state(state_path: Path | None, state: MonitorState) -> None:
    if not state_path:
        return
    state_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "start_event_id": state.start_event_id,
        "opened_pairs": sorted(state.opened_pairs),
        "closed_pairs": sorted(state.closed_pairs),
        "target_reached": state.target_reached,
        "alerted_keys": sorted(state.alerted_keys),
    }
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(state_path)


def fetch_json(url: str, timeout: float) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def check_status(
    status: dict[str, Any],
    state: MonitorState,
    args: argparse.Namespace,
    log_path: Path,
    now: float,
    alert_config: AlertConfig,
) -> None:
    snapshot = summarize_status(status)
    if snapshot["state"] != state.last_state:
        write_log(log_path, {"type": "state_change", **snapshot})
        state.last_state = snapshot["state"]
        if snapshot["state"] in {"UNHEDGED", "PAUSED"}:
            send_alert_once(
                alert_config,
                state,
                f"state:{snapshot['state']}",
                f"黄金执行器进入{snapshot['state']}状态",
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                log_path,
            )

    exposure_issue = exposure_issue_reason(status, args)
    if exposure_issue:
        issue_changed = state.exposure_issue_reason != exposure_issue
        if state.exposure_issue_since is None or issue_changed:
            state.exposure_issue_since = now
            state.exposure_issue_reason = exposure_issue
            write_log(log_path, {"type": "exposure_issue_seen", "issue": exposure_issue, **snapshot})
        elif now - state.exposure_issue_since >= args.single_leg_grace:
            write_log(
                log_path,
                {
                    "type": "exposure_issue_over_grace",
                    "issue": exposure_issue,
                    "seconds": round(now - state.exposure_issue_since, 3),
                    **snapshot,
                },
            )
            send_alert_once(
                alert_config,
                state,
                f"exposure:{exposure_issue}",
                "黄金持仓对冲异常超过宽限时间",
                f"{exposure_issue}\n\n当前摘要：{json.dumps(snapshot, ensure_ascii=False, sort_keys=True)}",
                log_path,
            )
    else:
        if state.exposure_issue_since is not None:
            write_log(log_path, {"type": "exposure_issue_recovered", "issue": state.exposure_issue_reason, **snapshot})
            state.alerted_keys.discard(f"exposure:{state.exposure_issue_reason}")
        state.exposure_issue_since = None
        state.exposure_issue_reason = None

    risk_issue = risk_warning_reason(status, args)
    if risk_issue:
        issue_changed = state.risk_issue_reason != risk_issue
        if state.risk_issue_since is None or issue_changed:
            state.risk_issue_since = now
            state.risk_issue_reason = risk_issue
            write_log(log_path, {"type": "risk_issue_seen", "issue": risk_issue, **snapshot})
        send_alert_once(
            alert_config,
            state,
            f"risk:{risk_issue}",
            "黄金持仓风险接近阈值",
            f"{risk_issue}\n\n当前摘要：{json.dumps(snapshot, ensure_ascii=False, sort_keys=True)}",
            log_path,
        )
    else:
        if state.risk_issue_since is not None:
            write_log(log_path, {"type": "risk_issue_recovered", "issue": state.risk_issue_reason, **snapshot})
            state.alerted_keys.discard(f"risk:{state.risk_issue_reason}")
        state.risk_issue_since = None
        state.risk_issue_reason = None

    profit_window = profit_window_reason(status, args)
    if profit_window:
        window_changed = state.profit_window_reason != profit_window
        if state.profit_window_since is None or window_changed:
            state.profit_window_since = now
            state.profit_window_reason = profit_window
            write_log(log_path, {"type": "profit_window_seen", "issue": profit_window, **snapshot})
        elif now - state.profit_window_since >= args.profit_window_grace:
            write_log(
                log_path,
                {
                    "type": "profit_window_over_grace",
                    "issue": profit_window,
                    "seconds": round(now - state.profit_window_since, 3),
                    **snapshot,
                },
            )
            send_alert_once(
                alert_config,
                state,
                f"profit_window:{profit_window}",
                "黄金出现正净值但未平仓",
                f"{profit_window}\n\n当前摘要：{json.dumps(snapshot, ensure_ascii=False, sort_keys=True)}",
                log_path,
            )
    else:
        if state.profit_window_since is not None:
            write_log(log_path, {"type": "profit_window_recovered", "issue": state.profit_window_reason, **snapshot})
            state.alerted_keys.discard(f"profit_window:{state.profit_window_reason}")
        state.profit_window_since = None
        state.profit_window_reason = None

    write_log(log_path, {"type": "tick", **snapshot})


def exposure_issue_reason(status: dict[str, Any], args: argparse.Namespace) -> str | None:
    binance_qty = parse_decimal(status.get("binance_position_qty"))
    mt4_positions = status.get("mt4_positions") or []
    mt4_oz = mt4_quantity_oz(status)
    has_binance = binance_qty != 0
    has_mt4 = mt4_oz != 0
    if has_binance != has_mt4:
        return "单腿持仓：币安和 MT4 只有一边有仓位"
    if not has_binance and not has_mt4:
        return None

    open_pair = status.get("open_pair") or {}
    direction = open_pair.get("direction")
    if direction == "BINANCE_SHORT_MT4_LONG":
        if binance_qty >= 0:
            return f"方向不一致：币安应为空单，实际数量 {binance_qty}"
        mt4_issue = mt4_side_issue(mt4_positions, "BUY")
        if mt4_issue:
            return mt4_issue
    elif direction == "BINANCE_LONG_MT4_SHORT":
        if binance_qty <= 0:
            return f"方向不一致：币安应为多单，实际数量 {binance_qty}"
        mt4_issue = mt4_side_issue(mt4_positions, "SELL")
        if mt4_issue:
            return mt4_issue

    tolerance = parse_decimal(getattr(args, "quantity_tolerance_oz", "0.01"))
    diff = abs(abs(binance_qty) - mt4_oz)
    if diff > tolerance:
        return f"数量不一致：币安 {abs(binance_qty)} XAU，MT4 {mt4_oz} XAU，差额 {diff} > 容忍 {tolerance}"
    return None


def risk_warning_reason(status: dict[str, Any], args: argparse.Namespace) -> str | None:
    metrics = status.get("position_metrics") or {}
    estimated_net = parse_decimal(metrics.get("estimated_close_net"))
    max_loss = parse_decimal(metrics.get("max_pair_loss_usdt"))
    if max_loss <= 0 or estimated_net >= 0:
        return None
    ratio = max(Decimal("0"), min(Decimal("1"), parse_decimal(getattr(args, "loss_warning_ratio", "0.70"))))
    warning_loss = max_loss * ratio
    if abs(estimated_net) < warning_loss:
        return None
    return f"预估净值 {estimated_net}U 已接近最大亏损 {max_loss}U 的 {ratio * Decimal('100')}%"


def profit_window_reason(status: dict[str, Any], args: argparse.Namespace) -> str | None:
    if status.get("state") != "PAIR_OPEN":
        return None
    execution_plan = status.get("execution_plan") or {}
    if bool(status.get("active_order")) or bool(execution_plan.get("active_binance_order")):
        return None
    gold_v2 = status.get("gold_v2") or {}
    exit_plan = gold_v2.get("exit_plan") or {}
    metrics = status.get("position_metrics") or {}
    estimated_net = parse_decimal(metrics.get("estimated_close_net"))
    min_profit = parse_decimal(getattr(args, "profit_window_min_usdt", "0.50"))
    if estimated_net < min_profit:
        return None
    target = parse_decimal(exit_plan.get("target_exit_spread"))
    current = parse_decimal(exit_plan.get("current_exit_spread") or metrics.get("current_exit_spread"))
    buffer_value = metrics.get("exit_follow_buffer_usd_per_oz")
    if target <= 0 and current > target:
        return f"预估净值 {estimated_net}U 已为正，但平仓目标被 MT4 跟随缓冲压到 {target}，当前平仓价差 {current}，缓冲 {buffer_value}"
    return None


def mt4_side_issue(positions: list[dict[str, Any]], expected_side: str) -> str | None:
    sides = sorted(
        {
            str(position.get("side") or "").upper()
            for position in positions
            if parse_decimal(position.get("lots")) != 0
        }
    )
    wrong = [side for side in sides if side and side != expected_side]
    if wrong:
        return f"方向不一致：MT4 应为 {expected_side}，实际 {','.join(wrong)}"
    return None


def summarize_status(status: dict[str, Any]) -> dict[str, Any]:
    gold_v2 = status.get("gold_v2") or {}
    selected = gold_v2.get("selected_entry") or {}
    exit_plan = gold_v2.get("exit_plan") or {}
    add_plan = gold_v2.get("add_plan") or {}
    metrics = status.get("position_metrics") or {}
    execution_plan = status.get("execution_plan") or {}
    active_binance_order = bool(status.get("active_order")) or bool(execution_plan.get("active_binance_order"))
    return {
        "state": status.get("state"),
        "last_error": status.get("last_error"),
        "binance_qty": status.get("binance_position_qty"),
        "mt4_count": len(status.get("mt4_positions") or []),
        "mt4_oz": str(mt4_quantity_oz(status)),
        "active_order": active_binance_order,
        "order_status": execution_plan.get("binance_order_status"),
        "order_side": execution_plan.get("binance_order_side"),
        "order_price": execution_plan.get("binance_order_price"),
        "order_qty": execution_plan.get("binance_order_qty"),
        "order_executed_qty": execution_plan.get("binance_order_executed_qty"),
        "current_edge": selected.get("current_edge"),
        "entry_threshold": selected.get("threshold"),
        "entry_ready": selected.get("ready"),
        "entry_reason": selected.get("reason"),
        "add_ready": add_plan.get("ready"),
        "add_reason": add_plan.get("reason"),
        "add_current_edge": add_plan.get("current_edge"),
        "add_next_trigger": add_plan.get("next_trigger_edge"),
        "add_count": add_plan.get("add_count"),
        "add_exit_viable": add_plan.get("exit_viable"),
        "actual_entry_spread": metrics.get("actual_entry_spread"),
        "current_exit_spread": metrics.get("current_exit_spread"),
        "target_exit_spread": exit_plan.get("target_exit_spread"),
        "estimated_close_net": metrics.get("estimated_close_net"),
    }


def mt4_quantity_oz(status: dict[str, Any]) -> Decimal:
    total = Decimal("0")
    for position in status.get("mt4_positions") or []:
        lots = parse_decimal(position.get("lots"))
        # Current gold setup uses 0.01 lot = 1 XAU. The monitor is read-only;
        # this conversion is only for single-leg detection.
        total += abs(lots) * Decimal("100")
    return total


def read_events(db_path: Path, after_id: int) -> list[sqlite3.Row]:
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT id, ts, kind, payload FROM events WHERE id > ? ORDER BY id",
            (after_id,),
        ).fetchall()


def handle_event(event: sqlite3.Row, state: MonitorState, log_path: Path, alert_config: AlertConfig) -> None:
    state.start_event_id = max(state.start_event_id, int(event["id"]))
    kind = event["kind"]
    payload = parse_payload(event["payload"])
    pair_id = str(payload.get("pair_id") or "")
    if kind == "v2_pair_open" and pair_id:
        state.opened_pairs.add(pair_id)
        write_log(log_path, {"type": "pair_open", "event_id": event["id"], "ts": event["ts"], "pair_id": pair_id, "payload": payload})
        send_alert_once(
            alert_config,
            state,
            f"open:{pair_id}",
            "黄金套利已开仓",
            f"黄金 V2 已开仓：{pair_id}\n\n{json.dumps(payload, ensure_ascii=False, sort_keys=True)}",
            log_path,
        )
    elif kind == "v2_pair_closed" and pair_id:
        state.closed_pairs.add(pair_id)
        write_log(
            log_path,
            {
                "type": "pair_closed",
                "event_id": event["id"],
                "ts": event["ts"],
                "pair_id": pair_id,
                "closed_cycles": len(state.closed_pairs),
                "payload": payload,
            },
        )
        send_alert_once(
            alert_config,
            state,
            f"close:{pair_id}",
            "黄金套利已平仓",
            f"黄金 V2 已平仓：{pair_id}\n已完成轮数：{len(state.closed_pairs)}\n\n{json.dumps(payload, ensure_ascii=False, sort_keys=True)}",
            log_path,
        )
    elif kind.startswith("v2_") or kind.startswith("runtime_"):
        write_log(log_path, {"type": "event", "event_id": event["id"], "ts": event["ts"], "kind": kind, "payload": payload})


def max_event_id(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
        return int(row[0] or 0)


def parse_payload(payload: str) -> dict[str, Any]:
    try:
        data = json.loads(payload)
        return data if isinstance(data, dict) else {"value": data}
    except json.JSONDecodeError:
        return {"raw": payload[:500]}


def parse_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def short_error(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def load_env_file(path: Path | None) -> None:
    if not path or not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


def load_alert_config() -> AlertConfig:
    recipients = tuple(
        item.strip()
        for item in os.getenv("GOLD_ALERT_EMAIL_TO", "").replace(";", ",").split(",")
        if item.strip()
    )
    username = os.getenv("GOLD_ALERT_SMTP_USER", "").strip()
    sender = os.getenv("GOLD_ALERT_EMAIL_FROM", "").strip() or username or "gold-v2-monitor@localhost"
    return AlertConfig(
        enabled=env_bool("GOLD_ALERT_EMAIL_ENABLED", default=False),
        host=os.getenv("GOLD_ALERT_SMTP_HOST", "").strip(),
        port=env_int("GOLD_ALERT_SMTP_PORT", default=587),
        username=username,
        password=os.getenv("GOLD_ALERT_SMTP_PASSWORD", ""),
        recipients=recipients,
        sender=sender,
        use_tls=env_bool("GOLD_ALERT_SMTP_TLS", default=True),
        use_ssl=env_bool("GOLD_ALERT_SMTP_SSL", default=False),
        timeout=env_float("GOLD_ALERT_SMTP_TIMEOUT", default=10.0),
    )


def send_alert_once(
    config: AlertConfig,
    state: MonitorState,
    key: str,
    subject: str,
    body: str,
    log_path: Path,
) -> None:
    if not config.ready or key in state.alerted_keys:
        return
    state.alerted_keys.add(key)
    try:
        send_email(config, subject, body)
        write_log(log_path, {"type": "alert_sent", "key": key, "subject": subject, "to": mask_recipients(config.recipients)})
    except Exception as exc:  # noqa: BLE001 - alerting must never stop monitoring.
        write_log(log_path, {"type": "alert_error", "key": key, "subject": subject, "error": short_error(exc)})


def send_email(config: AlertConfig, subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.sender
    message["To"] = ", ".join(config.recipients)
    message.set_content(body)

    smtp_class = smtplib.SMTP_SSL if config.use_ssl else smtplib.SMTP
    with smtp_class(config.host, config.port, timeout=config.timeout) as smtp:
        if config.use_tls and not config.use_ssl:
            smtp.starttls()
        if config.username:
            smtp.login(config.username, config.password)
        smtp.send_message(message)


def env_bool(key: str, default: bool) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def mask_recipients(recipients: tuple[str, ...]) -> list[str]:
    return [mask_email(item) for item in recipients]


def mask_email(value: str) -> str:
    if "@" not in value:
        return "***"
    name, domain = value.split("@", 1)
    if len(name) <= 2:
        masked_name = name[:1] + "***"
    else:
        masked_name = name[:2] + "***" + name[-1:]
    return f"{masked_name}@{domain}"


def configure_log_rotation(max_log_mb: float, backups: int) -> None:
    global LOG_MAX_BYTES
    global LOG_BACKUPS
    LOG_MAX_BYTES = max(0, int(max_log_mb * 1024 * 1024))
    LOG_BACKUPS = max(0, int(backups))


def configure_stdout_logging(enabled: bool) -> None:
    global STDOUT_LOGGING
    STDOUT_LOGGING = bool(enabled)


def rotate_log_if_needed(path: Path) -> None:
    if LOG_MAX_BYTES <= 0 or not path.exists():
        return
    try:
        if path.stat().st_size < LOG_MAX_BYTES:
            return
    except OSError:
        return
    if LOG_BACKUPS <= 0:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    oldest = rotated_log_path(path, LOG_BACKUPS)
    if oldest.exists():
        oldest.unlink()
    for index in range(LOG_BACKUPS - 1, 0, -1):
        src = rotated_log_path(path, index)
        if src.exists():
            src.replace(rotated_log_path(path, index + 1))
    path.replace(rotated_log_path(path, 1))


def rotated_log_path(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}")


def write_log(path: Path, data: dict[str, Any]) -> None:
    rotate_log_if_needed(path)
    data = {"ts": datetime.now(timezone.utc).isoformat(), **data}
    line = json.dumps(data, ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    if STDOUT_LOGGING:
        print(line, flush=True)


if __name__ == "__main__":
    sys.exit(main())
