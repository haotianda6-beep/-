import threading
import time
from dataclasses import dataclass

from app.core.models import BotSettings, ExchangeName
from app.services.cash_carry_executor import CashCarryExecutor
from app.services.cash_carry_fast_refresh import CashCarryFastRefresher
from app.services.cash_carry_positions import CashCarryPositionBuilder
from app.services.cash_carry_scanner import CashCarryScanner
from app.services.cross_spread_executor import CrossSpreadExecutor
from app.services.fast_opportunity_refresh import FastOpportunityRefresher
from app.services.live_market_types import CashCarryScan, LiveOpportunityScan
from app.services.live_opportunities import LiveOpportunityScanner
from app.services.live_read import LiveAccountSnapshot, LiveReadService
from app.services.mt4_bridge import Mt4SpreadScanner
from app.services.reverse_cash_carry_executor import ReverseCashCarryExecutor
from app.services.reverse_cash_carry_scanner import ReverseCashCarryScanner
from app.services.ws_ticker_cache import WSTickerCache


FAST_REFRESH_SECONDS = 1.0
FULL_SCAN_INTERVAL_SECONDS = 300.0
ACCOUNT_REFRESH_SECONDS = 30.0
MT4_SCAN_SECONDS = 5.0
STRATEGY_CROSS = "cross_spread"
STRATEGY_CASH = "cash_carry"
STRATEGY_REVERSE = "reverse_cash_carry"


@dataclass
class LiveRuntimeSnapshot:
    account: LiveAccountSnapshot
    scan: LiveOpportunityScan
    cash_carry: CashCarryScan
    reverse_cash_carry: CashCarryScan
    mt4_spread_opportunities: list
    mt4_spread_candidates: list
    mt4_spread_issues: list[str]


class LiveRuntimeCache:
    def __init__(
        self,
        live_read: LiveReadService,
        scanner: LiveOpportunityScanner,
        cash_carry_scanner: CashCarryScanner,
        reverse_cash_carry_scanner: ReverseCashCarryScanner,
        mt4_spread_scanner: Mt4SpreadScanner,
        cross_spread_executor: CrossSpreadExecutor | None = None,
        cash_carry_executor: CashCarryExecutor | None = None,
        reverse_cash_carry_executor: ReverseCashCarryExecutor | None = None,
        ticker_cache: WSTickerCache | None = None,
    ) -> None:
        self.live_read = live_read
        self.scanner = scanner
        self.cross_spread_executor = cross_spread_executor or CrossSpreadExecutor()
        self.cash_carry_scanner = cash_carry_scanner
        self.cash_carry_executor = cash_carry_executor or CashCarryExecutor()
        self.reverse_cash_carry_scanner = reverse_cash_carry_scanner
        self.mt4_spread_scanner = mt4_spread_scanner
        self.reverse_cash_carry_executor = reverse_cash_carry_executor or ReverseCashCarryExecutor()
        self.ticker_cache = ticker_cache or WSTickerCache()
        self.refresher = FastOpportunityRefresher(self.ticker_cache)
        self.cash_carry_refresher = CashCarryFastRefresher(self.ticker_cache, "forward")
        self.reverse_cash_carry_refresher = CashCarryFastRefresher(self.ticker_cache, "reverse")
        self.cash_position_builder = CashCarryPositionBuilder(self.ticker_cache)
        self._account = LiveAccountSnapshot(issues=["账户数据后台加载中"])
        self._scan = LiveOpportunityScan(issues=["机会扫描后台加载中"])
        self._cash_carry = CashCarryScan(issues=["期现扫描后台加载中"])
        self._reverse_cash_carry = CashCarryScan(issues=["反向期现扫描后台加载中"])
        self._mt4_spread_opportunities = []
        self._mt4_spread_candidates = []
        self._mt4_spread_issues = ["MT4 价差扫描后台加载中"]
        self._settings = BotSettings()
        self._lock = threading.Lock()
        self._execution_lock = threading.Lock()
        self._full_scan_lock = threading.Lock()
        self._started = False

    def get(self, settings: BotSettings) -> LiveRuntimeSnapshot:
        self._settings = settings
        self._ensure_started()
        with self._lock:
            return LiveRuntimeSnapshot(
                account=self._account,
                scan=self._scan,
                cash_carry=self._cash_carry,
                reverse_cash_carry=self._reverse_cash_carry,
                mt4_spread_opportunities=self._mt4_spread_opportunities,
                mt4_spread_candidates=self._mt4_spread_candidates,
                mt4_spread_issues=self._mt4_spread_issues,
            )

    def _ensure_started(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._account_loop, daemon=True, name="live-account-loop").start()
        threading.Thread(target=self._scan_loop, args=(5.0,), daemon=True, name="live-opportunity-loop").start()
        threading.Thread(target=self._cash_carry_loop, args=(20.0,), daemon=True, name="cash-carry-loop").start()
        threading.Thread(target=self._reverse_cash_carry_loop, args=(35.0,), daemon=True, name="reverse-cash-carry-loop").start()
        threading.Thread(target=self._mt4_spread_loop, args=(10.0,), daemon=True, name="mt4-spread-loop").start()

    def _account_loop(self) -> None:
        while True:
            if not self.live_read.live_data_enabled():
                time.sleep(FAST_REFRESH_SECONDS)
                continue
            result = self.live_read.fetch_account_snapshot()
            with self._lock:
                self._account = result
            time.sleep(ACCOUNT_REFRESH_SECONDS)

    def _scan_loop(self, initial_delay: float = 0.0) -> None:
        last_full_scan = 0.0
        time.sleep(initial_delay)
        while True:
            if not self.live_read.live_data_enabled():
                time.sleep(FAST_REFRESH_SECONDS)
                continue
            now = time.monotonic()
            with self._lock:
                current = self._scan
            if last_full_scan == 0.0 or now - last_full_scan >= FULL_SCAN_INTERVAL_SECONDS:
                result = self._run_full_scan(lambda: self.scanner.scan(self._settings), current)
                last_full_scan = time.monotonic()
            else:
                result = self.refresher.refresh(current, self._settings)
            with self._lock:
                self._scan = result
            self._execute_cross_spread(result)
            time.sleep(FAST_REFRESH_SECONDS)

    def _cash_carry_loop(self, initial_delay: float = 0.0) -> None:
        last_full_scan = 0.0
        time.sleep(initial_delay)
        while True:
            if not self.live_read.live_data_enabled():
                time.sleep(FAST_REFRESH_SECONDS)
                continue
            now = time.monotonic()
            if last_full_scan == 0.0 or now - last_full_scan >= FULL_SCAN_INTERVAL_SECONDS:
                with self._lock:
                    current = self._cash_carry
                result = self._run_full_scan(lambda: self.cash_carry_scanner.scan(self._settings), current)
                last_full_scan = time.monotonic()
                self._subscribe_cash_carry(result, self.cash_carry_scanner)
            else:
                with self._lock:
                    current = self._cash_carry
                result = self.cash_carry_refresher.refresh(current, self._settings)
            self._execute_cash_carry(result)
            with self._lock:
                self._cash_carry = result
            time.sleep(FAST_REFRESH_SECONDS)

    def _reverse_cash_carry_loop(self, initial_delay: float = 0.0) -> None:
        last_full_scan = 0.0
        time.sleep(initial_delay)
        while True:
            if not self.live_read.live_data_enabled():
                time.sleep(FAST_REFRESH_SECONDS)
                continue
            now = time.monotonic()
            if last_full_scan == 0.0 or now - last_full_scan >= FULL_SCAN_INTERVAL_SECONDS:
                with self._lock:
                    current = self._reverse_cash_carry
                result = self._run_full_scan(lambda: self.reverse_cash_carry_scanner.scan(self._settings), current)
                last_full_scan = time.monotonic()
                self._subscribe_cash_carry(result, self.reverse_cash_carry_scanner)
            else:
                with self._lock:
                    current = self._reverse_cash_carry
                result = self.reverse_cash_carry_refresher.refresh(current, self._settings)
            self._execute_reverse_cash_carry(result)
            with self._lock:
                self._reverse_cash_carry = result
            time.sleep(FAST_REFRESH_SECONDS)

    def _mt4_spread_loop(self, initial_delay: float = 0.0) -> None:
        time.sleep(initial_delay)
        while True:
            if not self.live_read.live_data_enabled():
                time.sleep(MT4_SCAN_SECONDS)
                continue
            opportunities, candidates, issues = self._run_full_scan(
                lambda: self.mt4_spread_scanner.scan(self._settings),
                (self._mt4_spread_opportunities, self._mt4_spread_candidates, self._mt4_spread_issues),
            )
            with self._lock:
                self._mt4_spread_opportunities = opportunities
                self._mt4_spread_candidates = candidates
                self._mt4_spread_issues = issues
            time.sleep(MT4_SCAN_SECONDS)

    def _run_full_scan(self, action, fallback):
        self._full_scan_lock.acquire()
        try:
            return action()
        finally:
            self._full_scan_lock.release()

    def _subscribe_cash_carry(self, scan: CashCarryScan, scanner: CashCarryScanner) -> None:
        for item in [*scan.opportunities, *scan.candidates]:
            exchange = item.exchange if isinstance(item.exchange, ExchangeName) else ExchangeName(item.exchange)
            spot_market, swap_market = scanner.market_pair(exchange, item.symbol)
            if spot_market:
                self.ticker_cache.subscribe(exchange, "spot", item.symbol, spot_market.ccxt_symbol)
            if swap_market:
                self.ticker_cache.subscribe(exchange, "swap", item.symbol, swap_market.ccxt_symbol)

    def _execute_cash_carry(self, result: CashCarryScan) -> None:
        rows = result.opportunities + result.candidates
        positions = self._cash_position_rows(rows)
        with self._execution_lock:
            self.cash_carry_executor.evaluate(
                rows,
                self._settings,
                positions,
                allow_open=self._auto_open_allowed(STRATEGY_CASH),
                allow_add=self._cash_carry_add_allowed(),
                allowed_open_exchanges=self._allowed_single_exchange_open_exchanges(),
            )

    def _execute_cross_spread(self, result: LiveOpportunityScan) -> None:
        with self._execution_lock:
            self.cross_spread_executor.evaluate(result.opportunities, self._settings, allow_open=self._auto_open_allowed(STRATEGY_CROSS))

    def _execute_reverse_cash_carry(self, result: CashCarryScan) -> None:
        rows = result.opportunities + result.candidates
        with self._execution_lock:
            self.reverse_cash_carry_executor.evaluate(rows, self._settings, allow_open=self._auto_open_allowed(STRATEGY_REVERSE), allowed_open_exchanges=self._allowed_single_exchange_open_exchanges())

    def _cash_position_rows(self, rows):
        with self._lock:
            positions = list(self._account.positions)
        return self.cash_position_builder.build(positions, rows, self._settings)

    def _auto_open_allowed(self, strategy: str | None = None) -> bool:
        with self._lock:
            if self._account.issues:
                return False
            live_positions = list(self._account.positions)
        if self._has_untracked_live_positions(live_positions):
            return False
        active = self._active_strategy_flags()
        if strategy is None:
            return not any(active.values())
        if strategy in {STRATEGY_CASH, STRATEGY_REVERSE}:
            return not active[STRATEGY_CROSS]
        return not any(enabled for name, enabled in active.items() if name != strategy)

    def _cash_carry_add_allowed(self) -> bool:
        return self.cash_carry_executor.has_active_records() and self._auto_open_allowed(STRATEGY_CASH)

    def _active_strategy_flags(self) -> dict[str, bool]:
        return {
            STRATEGY_CROSS: self.cross_spread_executor.has_active_records(),
            STRATEGY_CASH: self.cash_carry_executor.has_active_records(),
            STRATEGY_REVERSE: self.reverse_cash_carry_executor.has_active_records(),
        }

    def _allowed_single_exchange_open_exchanges(self) -> set[ExchangeName]:
        if not self._auto_open_allowed(STRATEGY_CASH):
            return set()
        used = self.cash_carry_executor.state.active_exchanges() | self.reverse_cash_carry_executor.active_exchanges()
        return set(ExchangeName) - used

    def _has_untracked_live_positions(self, positions) -> bool:
        tracked = self._tracked_live_position_keys()
        return any(item.quantity > 0 and (ExchangeName(item.exchange), item.symbol) not in tracked for item in positions)

    def _tracked_live_position_keys(self) -> set[tuple[ExchangeName, str]]:
        keys = set(self.cash_carry_executor.state.active_keys())
        keys.update(self.reverse_cash_carry_executor._active_keys())
        for record in self.cross_spread_executor.state.load_positions():
            keys.add((record.long_exchange, record.symbol))
            keys.add((record.short_exchange, record.symbol))
        return keys
