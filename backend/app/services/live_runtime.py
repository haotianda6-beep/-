import gc
import logging
import multiprocessing
import os
import threading
import time
from dataclasses import dataclass
from decimal import Decimal

from app.core.credential_utils import env_bool
from app.core.models import BotSettings, CashCarryOpportunity, ExchangeName
from app.services.binance_alpha_scanner import AlphaAlertScan, BinanceAlphaScanner
from app.services.cash_carry_scope import CASH_CARRY_EXCHANGE_SET, CASH_CARRY_INTERNAL_CANDIDATE_LIMIT
from app.services.cash_carry_executor import CashCarryExecutor
from app.services.cash_carry_fast_refresh import CashCarryFastRefresher
from app.services.cash_carry_positions import CashCarryPositionBuilder
from app.services.cash_carry_quality import cash_carry_candidate_sort_key, cash_carry_quality_score
from app.services.cash_carry_scanner import CashCarryScanner
from app.services.cash_carry_signal import CashCarrySignalTracker
from app.services.live_market_types import CashCarryScan
from app.services.live_read import LiveAccountSnapshot, LiveReadService
from app.services.mt4_bridge import Mt4SpreadScanner
from app.services.ws_ticker_cache import WSTickerCache


FAST_REFRESH_SECONDS = 1.0
FULL_SCAN_INTERVAL_SECONDS = 180.0
FULL_SCAN_TIMEOUT_SECONDS = 90.0
ACCOUNT_REFRESH_SECONDS = 30.0
MT4_SCAN_SECONDS = 2.0
ALPHA_ALERT_SCAN_SECONDS = 30.0
MEMORY_GUARD_SECONDS = 60.0
MEMORY_CLEANUP_RSS_MB = 1100.0
MEMORY_RESTART_RSS_MB = 1900.0
MEMORY_RESTART_GRACE_CYCLES = 3
STRATEGY_CASH = "cash_carry"
logger = logging.getLogger(__name__)


@dataclass
class LiveRuntimeSnapshot:
    account: LiveAccountSnapshot
    cash_carry: CashCarryScan
    alpha_alert: AlphaAlertScan
    mt4_spread_opportunities: list
    mt4_spread_candidates: list
    mt4_spread_issues: list[str]


@dataclass(frozen=True)
class TickerSubscription:
    exchange: ExchangeName
    symbol: str
    spot_ccxt_symbol: str
    swap_ccxt_symbol: str


@dataclass(frozen=True)
class CashCarryFullScanResult:
    scan: CashCarryScan
    subscriptions: list[TickerSubscription]


class LiveRuntimeCache:
    def __init__(
        self,
        live_read: LiveReadService,
        cash_carry_scanner: CashCarryScanner,
        mt4_spread_scanner: Mt4SpreadScanner,
        cash_carry_executor: CashCarryExecutor | None = None,
        ticker_cache: WSTickerCache | None = None,
    ) -> None:
        self.live_read = live_read
        self.cash_carry_scanner = cash_carry_scanner
        self.cash_carry_executor = cash_carry_executor or CashCarryExecutor()
        self.mt4_spread_scanner = mt4_spread_scanner
        self.ticker_cache = ticker_cache or WSTickerCache(max_symbols_per_stream=CASH_CARRY_INTERNAL_CANDIDATE_LIMIT)
        self.cash_carry_refresher = CashCarryFastRefresher(self.ticker_cache)
        self.cash_carry_signal_tracker = CashCarrySignalTracker()
        self.cash_position_builder = CashCarryPositionBuilder(self.ticker_cache)
        self.alpha_scanner = BinanceAlphaScanner()
        self._account = LiveAccountSnapshot(issues=["账户数据后台加载中"])
        self._cash_carry = CashCarryScan(issues=["期现扫描后台加载中"])
        self._alpha_alert = AlphaAlertScan(issues=["币安 Alpha 提醒后台加载中"])
        self._mt4_spread_opportunities = []
        self._mt4_spread_candidates = []
        self._mt4_spread_issues = ["MT4 价差扫描后台加载中"]
        self._settings = BotSettings()
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._execution_lock = threading.Lock()
        self._full_scan_slots = threading.BoundedSemaphore(1)
        self._mt4_scan_slots = threading.BoundedSemaphore(1)
        self._memory_restart_pressure = 0
        self._started = False

    def get(self, settings: BotSettings) -> LiveRuntimeSnapshot:
        self._settings = settings
        self._ensure_started()
        return self.cached()

    def cached(self) -> LiveRuntimeSnapshot:
        with self._lock:
            return LiveRuntimeSnapshot(
                account=self._account,
                cash_carry=self._cash_carry,
                alpha_alert=self._alpha_alert,
                mt4_spread_opportunities=self._mt4_spread_opportunities,
                mt4_spread_candidates=self._mt4_spread_candidates,
                mt4_spread_issues=self._mt4_spread_issues,
            )

    def _ensure_started(self) -> None:
        if self._started:
            return
        with self._start_lock:
            if self._started:
                return
            self._started = True
            threading.Thread(target=self._account_loop, daemon=True, name="live-account-loop").start()
            if _cash_carry_runtime_enabled():
                threading.Thread(target=self._cash_carry_loop, args=(20.0,), daemon=True, name="cash-carry-loop").start()
            if _mt4_spread_runtime_enabled():
                threading.Thread(target=self._mt4_spread_loop, args=(10.0,), daemon=True, name="mt4-spread-loop").start()
            threading.Thread(target=self._alpha_alert_loop, args=(5.0,), daemon=True, name="alpha-alert-loop").start()
            threading.Thread(target=self._memory_guard_loop, daemon=True, name="runtime-memory-guard").start()

    def _account_loop(self) -> None:
        while True:
            if not self.live_read.live_data_enabled():
                time.sleep(FAST_REFRESH_SECONDS)
                continue
            result = self.live_read.fetch_account_snapshot()
            with self._lock:
                self._account = result
            time.sleep(ACCOUNT_REFRESH_SECONDS)

    def _cash_carry_loop(self, initial_delay: float = 0.0) -> None:
        last_full_scan = 0.0
        time.sleep(initial_delay)
        while True:
            if not self.live_read.live_data_enabled():
                time.sleep(FAST_REFRESH_SECONDS)
                continue
            now = time.monotonic()
            if last_full_scan == 0.0 or now - last_full_scan >= _cash_carry_full_scan_interval():
                with self._lock:
                    current = self._cash_carry
                full_scan, completed = self._run_full_scan(lambda: self._cash_carry_full_scan(self._settings), current)
                result = full_scan.scan
                if completed:
                    last_full_scan = time.monotonic()
                    self._subscribe_cash_carry(full_scan.subscriptions)
                    self._drop_full_scan_caches()
            else:
                with self._lock:
                    current = self._cash_carry
                result = self.cash_carry_refresher.refresh(current, self._settings)
            result = self.cash_carry_signal_tracker.apply(result, self._settings)
            result = self._apply_cash_carry_open_scope(result)
            self._execute_cash_carry(result)
            with self._lock:
                self._cash_carry = result
            time.sleep(FAST_REFRESH_SECONDS)

    def _mt4_spread_loop(self, initial_delay: float = 0.0) -> None:
        time.sleep(initial_delay)
        while True:
            if not self.live_read.live_data_enabled():
                time.sleep(MT4_SCAN_SECONDS)
                continue
            result, _completed = self._run_guarded_scan(
                self._mt4_scan_slots,
                lambda: self.mt4_spread_scanner.scan(self._settings),
                (self._mt4_spread_opportunities, self._mt4_spread_candidates, self._mt4_spread_issues),
            )
            opportunities, candidates, issues = result
            with self._lock:
                self._mt4_spread_opportunities = opportunities
                self._mt4_spread_candidates = candidates
                self._mt4_spread_issues = issues
            time.sleep(MT4_SCAN_SECONDS)

    def _alpha_alert_loop(self, initial_delay: float = 0.0) -> None:
        time.sleep(initial_delay)
        while True:
            result = self.alpha_scanner.scan(self._settings)
            with self._lock:
                self._alpha_alert = result
            time.sleep(ALPHA_ALERT_SCAN_SECONDS)

    def _memory_guard_loop(self) -> None:
        while True:
            time.sleep(MEMORY_GUARD_SECONDS)
            rss_mb = self._process_rss_mb()
            if rss_mb is None:
                continue
            if rss_mb < MEMORY_CLEANUP_RSS_MB:
                self._memory_restart_pressure = 0
                continue
            before = rss_mb
            self._clear_runtime_caches()
            collected = gc.collect()
            after = self._process_rss_mb() or before
            logger.warning(
                "runtime memory cleanup: rss %.0fMB -> %.0fMB, collected=%s",
                before,
                after,
                collected,
            )
            if after >= MEMORY_RESTART_RSS_MB:
                self._memory_restart_pressure += 1
            else:
                self._memory_restart_pressure = 0
                continue
            if self._memory_restart_pressure < MEMORY_RESTART_GRACE_CYCLES:
                continue
            if not self._execution_lock.acquire(blocking=False):
                logger.warning("runtime memory restart delayed: execution lock is busy")
                continue
            logger.error("runtime memory above %.0fMB after cleanup; exiting for systemd restart", after)
            os._exit(75)

    def _clear_runtime_caches(self) -> None:
        cache_owners = (
            self.cash_carry_scanner,
            self.mt4_spread_scanner,
            self.cash_position_builder,
        )
        for owner in cache_owners:
            clear = getattr(owner, "clear_caches", None)
            if callable(clear):
                clear()
        self.ticker_cache.clear_caches(max_age_seconds=30)

    def _process_rss_mb(self) -> float | None:
        try:
            with open("/proc/self/status", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("VmRSS:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            return int(parts[1]) / 1024
        except OSError:
            return None
        return None

    def _drop_full_scan_caches(self) -> None:
        if not env_bool("MAIN_DASHBOARD_DROP_SCAN_CACHES", default=True):
            return
        self.cash_carry_scanner.clear_caches()
        gc.collect()

    def _run_full_scan(self, action, fallback: CashCarryScan):
        full_fallback = CashCarryFullScanResult(fallback, [])
        return self._run_guarded_scan(self._full_scan_slots, action, full_fallback)

    def _run_guarded_scan(self, slot: threading.BoundedSemaphore, action, fallback):
        if not slot.acquire(blocking=False):
            return fallback, False
        try:
            return action(), True
        except Exception as exc:  # noqa: BLE001 - keep background loops alive through exchange/client failures.
            logger.warning("live full scan failed: %s", str(exc)[:220])
            return fallback, False
        finally:
            slot.release()

    def _cash_carry_full_scan(self, settings: BotSettings) -> CashCarryFullScanResult:
        if not _cash_carry_scan_subprocess_enabled():
            scan = _compact_cash_carry_scan(self.cash_carry_scanner.scan(settings))
            return CashCarryFullScanResult(scan, _cash_carry_subscriptions(scan, self.cash_carry_scanner))
        return _cash_carry_full_scan_subprocess(settings)

    def _subscribe_cash_carry(self, subscriptions: list[TickerSubscription]) -> None:
        for item in subscriptions:
            self.ticker_cache.subscribe(item.exchange, "spot", item.symbol, item.spot_ccxt_symbol)
            self.ticker_cache.subscribe(item.exchange, "swap", item.symbol, item.swap_ccxt_symbol)

    def _apply_cash_carry_open_scope(self, scan: CashCarryScan) -> CashCarryScan:
        active_keys = self.cash_carry_executor.state.active_keys()
        active_counts = self.cash_carry_executor.state.active_counts_by_exchange()
        depth_reasons = self.cash_carry_executor.state.recent_depth_blocked_reasons(self.cash_carry_executor.depth_block_cooldown_seconds)
        items = [self._with_depth_block_reason(item, depth_reasons) for item in self._cash_carry_unique_items(scan)]
        if not active_counts:
            return self._rebuild_cash_carry_scan([self._without_open_scope_reason(item) for item in items], scan.issues)
        rows = []
        for item in items:
            exchange = ExchangeName(item.exchange)
            if (exchange, item.symbol) in active_keys:
                rows.append(self._with_open_scope_reason(item, "该交易所该币种已有正向期现持仓，禁止重复开仓"))
                continue
            active_count = active_counts.get(exchange, 0)
            if active_count < self._settings.cash_carry_max_positions_per_exchange:
                rows.append(self._without_open_scope_reason(item))
                continue
            reason = f"同交易所正向期现持仓槽位已满 {active_count}/{self._settings.cash_carry_max_positions_per_exchange}"
            rows.append(self._with_open_scope_reason(item, reason))
        return self._rebuild_cash_carry_scan(rows, scan.issues)

    def _with_depth_block_reason(
        self,
        item: CashCarryOpportunity,
        depth_reasons: dict[tuple[ExchangeName, str], str],
    ) -> CashCarryOpportunity:
        reason = depth_reasons.get((ExchangeName(item.exchange), item.symbol))
        if not reason:
            return item
        reasons = [*item.blocked_reasons, reason]
        return item.model_copy(update={"blocked_reasons": self._dedupe_reasons(reasons)})

    def _cash_carry_unique_items(self, scan: CashCarryScan) -> list[CashCarryOpportunity]:
        seen: set[tuple[ExchangeName, str]] = set()
        result = []
        for item in [*scan.opportunities, *scan.candidates]:
            key = (ExchangeName(item.exchange), item.symbol)
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    def _without_open_scope_reason(self, item: CashCarryOpportunity) -> CashCarryOpportunity:
        reasons = [reason for reason in item.blocked_reasons if not self._is_open_scope_reason(reason)]
        return item.model_copy(update={"blocked_reasons": reasons})

    def _with_open_scope_reason(self, item: CashCarryOpportunity, reason: str) -> CashCarryOpportunity:
        reasons = [reason for reason in self._without_open_scope_reason(item).blocked_reasons if reason]
        reasons.append(reason)
        return item.model_copy(update={"blocked_reasons": self._dedupe_reasons(reasons)})

    def _is_open_scope_reason(self, reason: str) -> bool:
        return "一所一币规则" in reason or "已有正向期现持仓" in reason or "持仓槽位已满" in reason

    def _rebuild_cash_carry_scan(self, rows: list[CashCarryOpportunity], issues: list[str]) -> CashCarryScan:
        opportunities = [item for item in rows if not item.blocked_reasons]
        candidates = sorted(rows, key=self._cash_carry_candidate_sort_key)[:CASH_CARRY_INTERNAL_CANDIDATE_LIMIT]
        return CashCarryScan(
            opportunities=sorted(opportunities, key=self._cash_carry_opportunity_sort_key),
            candidates=candidates,
            issues=issues,
        )

    def _cash_carry_candidate_sort_key(self, item: CashCarryOpportunity) -> tuple[int, int, Decimal, Decimal, Decimal, Decimal]:
        return cash_carry_candidate_sort_key(
            self._settings,
            item.blocked_reasons,
            item.basis_pct,
            item.estimated_net_profit,
            self._cash_carry_quality_score(item),
        )

    def _cash_carry_opportunity_sort_key(self, item: CashCarryOpportunity) -> tuple[Decimal, Decimal]:
        return (-self._cash_carry_quality_score(item), -item.estimated_net_profit)

    def _cash_carry_quality_score(self, item: CashCarryOpportunity) -> Decimal:
        return cash_carry_quality_score(
            self._settings,
            item.basis_pct,
            item.funding_rate_pct / Decimal("100"),
            min(item.spot_volume_24h_usdt, item.perp_volume_24h_usdt),
            item.estimated_net_profit,
            item.max_safe_notional_usdt,
        )

    def _dedupe_reasons(self, reasons: list[str]) -> list[str]:
        result = []
        seen = set()
        for reason in reasons:
            if reason in seen:
                continue
            seen.add(reason)
            result.append(reason)
        return result

    def _execute_cash_carry(self, result: CashCarryScan) -> None:
        rows = result.opportunities + result.candidates
        try:
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
        except Exception as exc:  # noqa: BLE001 - exchange execution failures must not stop scanning.
            logger.warning("cash carry execution failed: %s", str(exc)[:220])

    def _cash_position_rows(self, rows):
        with self._lock:
            positions = list(self._account.positions)
        return self.cash_position_builder.build(positions, rows, self._settings)

    def _auto_open_allowed(self, strategy: str | None = None) -> bool:
        with self._lock:
            account_has_issues = bool(self._account.issues)
            healthy_exchanges = self._healthy_account_exchanges_locked()
            live_positions = list(self._account.positions)
        healthy_cash_exchanges = healthy_exchanges & CASH_CARRY_EXCHANGE_SET
        if not healthy_cash_exchanges:
            return False
        if self._has_untracked_live_positions(live_positions):
            return False
        active = self._active_strategy_flags()
        if strategy is None:
            if account_has_issues:
                return False
            return not any(active.values())
        if strategy == STRATEGY_CASH:
            return True
        if account_has_issues:
            return False
        return False

    def _cash_carry_add_allowed(self) -> bool:
        return self.cash_carry_executor.has_active_records() and self._auto_open_allowed(STRATEGY_CASH)

    def _active_strategy_flags(self) -> dict[str, bool]:
        return {
            STRATEGY_CASH: self.cash_carry_executor.has_active_records(),
        }

    def _allowed_single_exchange_open_exchanges(self) -> set[ExchangeName]:
        if not self._auto_open_allowed(STRATEGY_CASH):
            return set()
        with self._lock:
            healthy = self._healthy_account_exchanges_locked()
        counts = self.cash_carry_executor.state.active_counts_by_exchange()
        return {
            exchange
            for exchange in (healthy & CASH_CARRY_EXCHANGE_SET)
            if counts.get(exchange, 0) < self._settings.cash_carry_max_positions_per_exchange
        }

    def _healthy_account_exchanges_locked(self) -> set[ExchangeName]:
        return {ExchangeName(item.exchange) for item in self._account.balances}

    def _has_untracked_live_positions(self, positions) -> bool:
        tracked = self._tracked_live_position_keys()
        return any(
            item.quantity > 0
            and ExchangeName(item.exchange) in CASH_CARRY_EXCHANGE_SET
            and (ExchangeName(item.exchange), item.symbol) not in tracked
            for item in positions
        )

    def _tracked_live_position_keys(self) -> set[tuple[ExchangeName, str]]:
        return set(self.cash_carry_executor.state.active_keys())


def _cash_carry_runtime_enabled() -> bool:
    return env_bool("MAIN_DASHBOARD_CASH_CARRY_RUNTIME", default=True)


def _cash_carry_full_scan_interval() -> float:
    try:
        value = float(os.getenv("MAIN_DASHBOARD_CASH_CARRY_FULL_SCAN_SECONDS", str(FULL_SCAN_INTERVAL_SECONDS)))
    except ValueError:
        return FULL_SCAN_INTERVAL_SECONDS
    return max(60.0, value)


def _cash_carry_scan_subprocess_enabled() -> bool:
    return env_bool("MAIN_DASHBOARD_CASH_CARRY_SCAN_SUBPROCESS", default=True)


def _mt4_spread_runtime_enabled() -> bool:
    lightweight = env_bool("MAIN_DASHBOARD_LIGHTWEIGHT", default=True)
    return env_bool("MAIN_DASHBOARD_MT4_SPREAD_RUNTIME", default=not lightweight)


def _cash_carry_full_scan_subprocess(settings: BotSettings) -> CashCarryFullScanResult:
    timeout = _cash_carry_scan_timeout()
    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_cash_carry_scan_worker, args=(settings, child_conn), daemon=True)
    process.start()
    child_conn.close()
    try:
        if not parent_conn.poll(timeout):
            raise TimeoutError(f"正向期现全量扫描超过 {timeout:.0f} 秒，已终止本次扫描")
        kind, payload = parent_conn.recv()
    except TimeoutError:
        process.terminate()
        process.join(timeout=5)
        raise
    finally:
        parent_conn.close()
        if process.is_alive():
            process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    if kind == "error":
        raise RuntimeError(str(payload))
    return payload


def _cash_carry_scan_timeout() -> float:
    raw = os.getenv("MAIN_DASHBOARD_FULL_SCAN_TIMEOUT_SECONDS", str(FULL_SCAN_TIMEOUT_SECONDS)).strip()
    try:
        value = float(Decimal(raw))
    except Exception:
        return FULL_SCAN_TIMEOUT_SECONDS
    return max(10.0, value)


def _cash_carry_scan_worker(settings: BotSettings, result_conn) -> None:
    scanner = CashCarryScanner()
    try:
        scan = _compact_cash_carry_scan(scanner.scan(settings))
        result_conn.send(("ok", CashCarryFullScanResult(scan, _cash_carry_subscriptions(scan, scanner))))
    except Exception as exc:  # noqa: BLE001 - return sanitized worker failure to parent process.
        result_conn.send(("error", str(exc)[:220]))
    finally:
        result_conn.close()


def _cash_carry_subscriptions(scan: CashCarryScan, scanner: CashCarryScanner) -> list[TickerSubscription]:
    subscriptions: list[TickerSubscription] = []
    seen: set[tuple[ExchangeName, str]] = set()
    for item in [*scan.opportunities, *scan.candidates]:
        exchange = item.exchange if isinstance(item.exchange, ExchangeName) else ExchangeName(item.exchange)
        key = (exchange, item.symbol)
        if key in seen:
            continue
        seen.add(key)
        spot_market, swap_market = scanner.market_pair(exchange, item.symbol)
        if spot_market and swap_market:
            subscriptions.append(
                TickerSubscription(
                    exchange=exchange,
                    symbol=item.symbol,
                    spot_ccxt_symbol=spot_market.ccxt_symbol,
                    swap_ccxt_symbol=swap_market.ccxt_symbol,
                )
            )
    return subscriptions


def _compact_cash_carry_scan(scan: CashCarryScan, opportunity_limit: int = 50, candidate_limit: int = CASH_CARRY_INTERNAL_CANDIDATE_LIMIT) -> CashCarryScan:
    return CashCarryScan(
        opportunities=list(scan.opportunities[:opportunity_limit]),
        candidates=list(scan.candidates[:candidate_limit]),
        issues=list(scan.issues[:30]),
    )
