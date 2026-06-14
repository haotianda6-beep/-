import logging
import multiprocessing
import os
import threading
import time
from collections.abc import Callable

from app.core.env import env_bool
from app.core.models import BotSettings
from app.services.live_market_types import LiveOpportunityScan
from app.services.live_opportunities import LiveOpportunityScanner


logger = logging.getLogger(__name__)


class CrossSpreadScanCache:
    def __init__(
        self,
        scanner: LiveOpportunityScanner,
        live_data_enabled: Callable[[], bool],
        ttl_seconds: float = 300.0,
    ) -> None:
        self.scanner = scanner
        self.live_data_enabled = live_data_enabled
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._cache = LiveOpportunityScan(issues=["五所价差扫描后台加载中"])
        self._cache_at = 0.0
        self._refreshing = False

    def get(self, settings: BotSettings) -> LiveOpportunityScan:
        if not self.live_data_enabled():
            return LiveOpportunityScan(issues=["实盘数据读取未启用，五所价差扫描未启动"])
        if not env_bool("CROSS_SPREAD_SCAN_ENABLED", default=True):
            return LiveOpportunityScan(issues=["五所价差扫描已通过 CROSS_SPREAD_SCAN_ENABLED 关闭"])
        self._start_refresh(settings)
        with self._lock:
            return LiveOpportunityScan(
                opportunities=list(self._cache.opportunities),
                candidates=list(self._cache.candidates),
                issues=list(self._cache.issues),
            )

    def _start_refresh(self, settings: BotSettings) -> None:
        now = time.monotonic()
        with self._lock:
            if self._refreshing:
                return
            if self._cache_at and now - self._cache_at < self.ttl_seconds:
                return
            self._refreshing = True
        threading.Thread(
            target=self._refresh,
            args=(settings.model_copy(deep=True),),
            daemon=True,
            name="cross-spread-scan-refresh",
        ).start()

    def _refresh(self, settings: BotSettings) -> None:
        try:
            result = self._run_scan(settings)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cross spread scan failed: %s", str(exc)[:220])
            with self._lock:
                self._cache = LiveOpportunityScan(
                    opportunities=list(self._cache.opportunities),
                    candidates=list(self._cache.candidates),
                    issues=[f"五所价差扫描失败 {str(exc)[:220]}"],
                )
                self._cache_at = time.monotonic()
                self._refreshing = False
            return
        with self._lock:
            self._cache = result
            self._cache_at = time.monotonic()
            self._refreshing = False

    def _run_scan(self, settings: BotSettings) -> LiveOpportunityScan:
        if not env_bool("CROSS_SPREAD_SCAN_SUBPROCESS", default=True):
            return _compact_cross_spread_scan(self.scanner.scan(settings))
        return _cross_spread_scan_subprocess(settings)


def _cross_spread_scan_subprocess(settings: BotSettings) -> LiveOpportunityScan:
    timeout = _cross_spread_scan_timeout()
    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_cross_spread_scan_worker, args=(settings, child_conn), daemon=True)
    process.start()
    child_conn.close()
    try:
        if not parent_conn.poll(timeout):
            raise TimeoutError(f"五所价差全量扫描超过 {timeout:.0f} 秒，已终止本次扫描")
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


def _cross_spread_scan_timeout() -> float:
    raw = os.getenv("CROSS_SPREAD_SCAN_TIMEOUT_SECONDS", "120").strip()
    try:
        value = float(raw)
    except ValueError:
        return 120.0
    return max(20.0, value)


def _cross_spread_scan_worker(settings: BotSettings, result_conn) -> None:
    scanner = LiveOpportunityScanner()
    try:
        result_conn.send(("ok", _compact_cross_spread_scan(scanner.scan(settings))))
    except Exception as exc:  # noqa: BLE001
        result_conn.send(("error", str(exc)[:220]))
    finally:
        result_conn.close()


def _compact_cross_spread_scan(scan: LiveOpportunityScan) -> LiveOpportunityScan:
    return LiveOpportunityScan(
        opportunities=list(scan.opportunities[:25]),
        candidates=list(scan.candidates[:50]),
        issues=list(scan.issues[:30]),
    )
