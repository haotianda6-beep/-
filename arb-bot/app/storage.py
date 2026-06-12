from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.models import HistoryBar


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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS market_bars (
                    source TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    open_time_ms INTEGER NOT NULL,
                    open TEXT NOT NULL,
                    high TEXT NOT NULL,
                    low TEXT NOT NULL,
                    close TEXT NOT NULL,
                    volume TEXT,
                    received_ts TEXT NOT NULL,
                    PRIMARY KEY (source, symbol, interval, open_time_ms)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_market_bars_lookup
                ON market_bars (source, symbol, interval, open_time_ms)
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

    def upsert_bars(self, source: str, symbol: str, interval: str, bars: list[HistoryBar]) -> int:
        if not bars:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                source,
                symbol,
                interval,
                int(bar.open_time_ms),
                str(bar.open),
                str(bar.high),
                str(bar.low),
                str(bar.close),
                str(bar.volume) if bar.volume is not None else None,
                now,
            )
            for bar in bars
        ]
        with self._lock, self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO market_bars (
                    source, symbol, interval, open_time_ms, open, high, low, close, volume, received_ts
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, symbol, interval, open_time_ms)
                DO UPDATE SET
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    received_ts=excluded.received_ts
                """,
                rows,
            )
        return len(rows)

    def get_bars(
        self,
        source: str,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> list[HistoryBar]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT open_time_ms, open, high, low, close, volume
                FROM market_bars
                WHERE source = ? AND symbol = ? AND interval = ? AND open_time_ms >= ? AND open_time_ms <= ?
                ORDER BY open_time_ms ASC
                """,
                (source, symbol, interval, start_ms, end_ms),
            ).fetchall()
        return [
            HistoryBar(
                open_time_ms=int(row[0]),
                open=Decimal(row[1]),
                high=Decimal(row[2]),
                low=Decimal(row[3]),
                close=Decimal(row[4]),
                volume=Decimal(row[5]) if row[5] is not None else None,
            )
            for row in rows
        ]

    def bar_count(self, source: str, symbol: str, interval: str) -> int:
        with self._lock, self._connect() as conn:
            return int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM market_bars
                    WHERE source = ? AND symbol = ? AND interval = ?
                    """,
                    (source, symbol, interval),
                ).fetchone()[0]
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)
