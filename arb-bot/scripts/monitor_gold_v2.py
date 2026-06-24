#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


@dataclass
class MonitorState:
    start_event_id: int
    opened_pairs: set[str]
    closed_pairs: set[str]
    single_leg_since: float | None = None
    last_state: str | None = None


def main() -> int:
    args = parse_args()
    log_path = Path(args.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    start_id = max_event_id(Path(args.db))
    state = MonitorState(start_event_id=start_id, opened_pairs=set(), closed_pairs=set())
    deadline = time.monotonic() + args.max_minutes * 60
    write_log(log_path, {"type": "monitor_start", "start_event_id": start_id, "cycles_target": args.cycles})

    while time.monotonic() < deadline:
        now = time.monotonic()
        try:
            status = fetch_json(args.status_url, args.http_timeout)
            check_status(status, state, args, log_path, now)
        except Exception as exc:
            write_log(log_path, {"type": "status_error", "error": short_error(exc)})

        events = read_events(Path(args.db), state.start_event_id)
        for event in events:
            handle_event(event, state, log_path)
        if len(state.closed_pairs) >= args.cycles:
            write_log(log_path, {"type": "monitor_done", "closed_cycles": len(state.closed_pairs)})
            return 0
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
    return parser.parse_args()


def fetch_json(url: str, timeout: float) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def check_status(
    status: dict[str, Any],
    state: MonitorState,
    args: argparse.Namespace,
    log_path: Path,
    now: float,
) -> None:
    snapshot = summarize_status(status)
    if snapshot["state"] != state.last_state:
        write_log(log_path, {"type": "state_change", **snapshot})
        state.last_state = snapshot["state"]

    binance_qty = parse_decimal(status.get("binance_position_qty"))
    mt4_oz = mt4_quantity_oz(status)
    has_binance = binance_qty != 0
    has_mt4 = mt4_oz != 0
    single_leg = has_binance != has_mt4
    if single_leg:
        if state.single_leg_since is None:
            state.single_leg_since = now
            write_log(log_path, {"type": "single_leg_seen", **snapshot})
        elif now - state.single_leg_since >= args.single_leg_grace:
            write_log(log_path, {"type": "single_leg_over_grace", "seconds": round(now - state.single_leg_since, 3), **snapshot})
    else:
        if state.single_leg_since is not None:
            write_log(log_path, {"type": "single_leg_recovered", **snapshot})
        state.single_leg_since = None

    write_log(log_path, {"type": "tick", **snapshot})


def summarize_status(status: dict[str, Any]) -> dict[str, Any]:
    gold_v2 = status.get("gold_v2") or {}
    selected = gold_v2.get("selected_entry") or {}
    exit_plan = gold_v2.get("exit_plan") or {}
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


def handle_event(event: sqlite3.Row, state: MonitorState, log_path: Path) -> None:
    state.start_event_id = max(state.start_event_id, int(event["id"]))
    kind = event["kind"]
    payload = parse_payload(event["payload"])
    pair_id = str(payload.get("pair_id") or "")
    if kind == "v2_pair_open" and pair_id:
        state.opened_pairs.add(pair_id)
        write_log(log_path, {"type": "pair_open", "event_id": event["id"], "ts": event["ts"], "pair_id": pair_id, "payload": payload})
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


def write_log(path: Path, data: dict[str, Any]) -> None:
    data = {"ts": datetime.now(timezone.utc).isoformat(), **data}
    line = json.dumps(data, ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


if __name__ == "__main__":
    sys.exit(main())
