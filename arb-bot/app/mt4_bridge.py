from __future__ import annotations

import threading
from collections import deque
from decimal import Decimal

from app.config import Settings
from app.models import AccountSnapshot, MarketQuote, Mt4Command, Mt4Report, Mt4SwapInfo, Mt4Tick, Side, utc_now_ms


class Mt4Bridge:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._quote: MarketQuote | None = None
        self._quote_history: deque[MarketQuote] = deque(maxlen=2000)
        self._commands: deque[Mt4Command] = deque()
        self._pending: dict[str, Mt4Command] = {}
        self._reports: deque[Mt4Report] = deque()
        self._positions = []
        self._swap_info = Mt4SwapInfo()
        self._account: AccountSnapshot | None = None
        self.last_seen_ms = 0

    def token_ok(self, token: str | None) -> bool:
        expected = self.settings.mt4_bridge_token
        if expected is None:
            return True
        return (token or "") == expected.get_secret_value()

    def update_tick(self, tick: Mt4Tick) -> MarketQuote:
        received_ms = utc_now_ms()
        quote = MarketQuote(symbol=tick.symbol, bid=tick.bid, ask=tick.ask, timestamp_ms=received_ms)
        with self._lock:
            self._quote = quote
            self._quote_history.append(quote)
            self._positions = list(tick.positions)
            self._swap_info = Mt4SwapInfo(
                swap_long_per_lot=tick.swap_long_per_lot,
                swap_short_per_lot=tick.swap_short_per_lot,
                swap_type=tick.swap_type,
                tick_value=tick.tick_value,
                tick_size=tick.tick_size,
                point=tick.point,
                next_rollover_time_ms=tick.next_rollover_time_ms,
            )
            account_values = (
                tick.account_balance,
                tick.account_equity,
                tick.account_free_margin,
                tick.account_margin,
                tick.account_profit,
            )
            self._account = (
                AccountSnapshot(
                    venue="MT4",
                    balance=tick.account_balance,
                    equity=tick.account_equity,
                    available=tick.account_free_margin,
                    used_margin=tick.account_margin,
                    unrealized_pnl=tick.account_profit,
                    currency=tick.account_currency,
                    timestamp_ms=received_ms,
                )
                if any(value is not None for value in account_values)
                else None
            )
            self.last_seen_ms = received_ms
        return quote

    def latest_quote(self) -> MarketQuote | None:
        with self._lock:
            return self._quote

    def recent_move_budget(self, lookback_ms: int, percentile: int = 70, min_points: int = 8) -> Decimal | None:
        now = utc_now_ms()
        cutoff = now - max(int(lookback_ms), 0)
        with self._lock:
            quotes = [quote for quote in self._quote_history if quote.timestamp_ms >= cutoff]
        if len(quotes) < min_points:
            return None
        quotes.sort(key=lambda quote: quote.timestamp_ms)
        moves = [abs(curr.bid - prev.bid) for prev, curr in zip(quotes, quotes[1:])]
        if not moves:
            return None
        moves.sort()
        bounded_percentile = min(max(percentile, 0), 100)
        index = ((len(moves) - 1) * bounded_percentile) // 100
        return moves[index]

    def latest_swap_info(self) -> Mt4SwapInfo:
        with self._lock:
            return self._swap_info

    def positions(self) -> list:
        with self._lock:
            return list(self._positions)

    def account_snapshot(self) -> AccountSnapshot | None:
        with self._lock:
            return self._account

    def connected(self, max_age_ms: int = 3000) -> bool:
        with self._lock:
            return self.last_seen_ms > 0 and utc_now_ms() - self.last_seen_ms <= max_age_ms

    def queue_market_order(
        self,
        side: Side,
        lots: Decimal,
        reason: str,
        max_price: Decimal | None = None,
        min_price: Decimal | None = None,
    ) -> Mt4Command:
        command = Mt4Command(
            action=side.value,
            symbol=self.settings.mt4_symbol,
            lots=lots,
            slippage_points=self.settings.mt4_slippage_points,
            max_price=max_price,
            min_price=min_price,
            reason=reason,
        )
        with self._lock:
            self._commands.append(command)
            self._pending[command.command_id] = command
        return command

    def queue_close(
        self,
        ticket: int,
        lots: Decimal,
        reason: str,
        max_price: Decimal | None = None,
        min_price: Decimal | None = None,
    ) -> Mt4Command:
        command = Mt4Command(
            action="CLOSE",
            symbol=self.settings.mt4_symbol,
            lots=lots,
            slippage_points=self.settings.mt4_slippage_points,
            max_price=max_price,
            min_price=min_price,
            ticket=ticket,
            reason=reason,
        )
        with self._lock:
            self._commands.append(command)
            self._pending[command.command_id] = command
        return command

    def next_command(self) -> dict:
        with self._lock:
            if not self._commands:
                return {"command": "NONE"}
            command = self._commands.popleft()
            return command.model_dump(mode="json")

    def submit_report(self, report: Mt4Report) -> None:
        with self._lock:
            self._reports.append(report)
            self._pending.pop(report.command_id, None)

    def drain_reports(self) -> list[Mt4Report]:
        with self._lock:
            reports = list(self._reports)
            self._reports.clear()
            return reports

    def pending_command(self, command_id: str) -> Mt4Command | None:
        with self._lock:
            return self._pending.get(command_id)
