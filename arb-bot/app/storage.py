from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.models import HistoryBar, Mt4ClosedOrder


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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mt4_closed_orders (
                    ticket INTEGER PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    lots TEXT NOT NULL,
                    open_time_ms INTEGER NOT NULL,
                    close_time_ms INTEGER NOT NULL,
                    open_price TEXT NOT NULL,
                    close_price TEXT NOT NULL,
                    profit TEXT NOT NULL,
                    swap TEXT NOT NULL,
                    commission TEXT NOT NULL,
                    magic_number INTEGER,
                    comment TEXT,
                    received_ts TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_mt4_closed_orders_lookup
                ON mt4_closed_orders (symbol, close_time_ms)
                """
            )

    def record_event(self, kind: str, payload: dict[str, Any]) -> None:
        clean = json.dumps(payload, ensure_ascii=False, default=str)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO events (ts, kind, payload) VALUES (?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), kind, clean),
            )

    def get_events(self, start_ms: int, end_ms: int, limit: int = 5000) -> list[dict[str, Any]]:
        start_iso = datetime.fromtimestamp(start_ms / 1000, timezone.utc).isoformat()
        end_iso = datetime.fromtimestamp(end_ms / 1000, timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, ts, kind, payload
                FROM events
                WHERE ts >= ? AND ts <= ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (start_iso, end_iso, limit),
            ).fetchall()
        events = []
        for row in rows:
            try:
                payload = json.loads(row[3])
            except json.JSONDecodeError:
                payload = {}
            events.append({"id": int(row[0]), "ts": row[1], "kind": row[2], "payload": payload})
        return events

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

    def upsert_mt4_closed_orders(self, orders: list[Mt4ClosedOrder]) -> int:
        if not orders:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                order.ticket,
                order.symbol,
                order.side.value,
                str(order.lots),
                int(order.open_time_ms),
                int(order.close_time_ms),
                str(order.open_price),
                str(order.close_price),
                str(order.profit),
                str(order.swap),
                str(order.commission),
                order.magic_number,
                order.comment,
                now,
            )
            for order in orders
        ]
        with self._lock, self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO mt4_closed_orders (
                    ticket, symbol, side, lots, open_time_ms, close_time_ms, open_price, close_price,
                    profit, swap, commission, magic_number, comment, received_ts
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticket)
                DO UPDATE SET
                    symbol=excluded.symbol,
                    side=excluded.side,
                    lots=excluded.lots,
                    open_time_ms=excluded.open_time_ms,
                    close_time_ms=excluded.close_time_ms,
                    open_price=excluded.open_price,
                    close_price=excluded.close_price,
                    profit=excluded.profit,
                    swap=excluded.swap,
                    commission=excluded.commission,
                    magic_number=excluded.magic_number,
                    comment=excluded.comment,
                    received_ts=excluded.received_ts
                """,
                rows,
            )
        return len(rows)

    def get_mt4_closed_orders(self, symbol: str, start_ms: int, end_ms: int, limit: int = 100) -> list[Mt4ClosedOrder]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ticket, symbol, side, lots, open_time_ms, close_time_ms, open_price, close_price,
                       profit, swap, commission, magic_number, comment
                FROM mt4_closed_orders
                WHERE symbol = ? AND close_time_ms >= ? AND close_time_ms <= ?
                ORDER BY close_time_ms DESC
                LIMIT ?
                """,
                (symbol, start_ms, end_ms, limit),
            ).fetchall()
        return [
            Mt4ClosedOrder(
                ticket=int(row[0]),
                symbol=row[1],
                side=row[2],
                lots=Decimal(row[3]),
                open_time_ms=int(row[4]),
                close_time_ms=int(row[5]),
                open_price=Decimal(row[6]),
                close_price=Decimal(row[7]),
                profit=Decimal(row[8]),
                swap=Decimal(row[9]),
                commission=Decimal(row[10]),
                magic_number=int(row[11]) if row[11] is not None else None,
                comment=row[12],
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
