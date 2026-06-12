from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pnl (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    pair_id TEXT NOT NULL,
                    realized_pnl TEXT NOT NULL
                )
                """
            )

    def record_event(self, kind: str, payload: dict[str, Any]) -> None:
        clean = json.dumps(payload, ensure_ascii=False, default=str)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO events (ts, kind, payload) VALUES (?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), kind, clean),
            )

    def record_pnl(self, pair_id: str, pnl: Decimal) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO pnl (ts, pair_id, realized_pnl) VALUES (?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), pair_id, str(pnl)),
            )

    def daily_pnl(self) -> Decimal:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT realized_pnl FROM pnl WHERE ts >= ?", (today,)).fetchall()
        return sum((Decimal(row[0]) for row in rows), Decimal("0"))

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

