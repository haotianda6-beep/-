from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import httpx

from app.config import Settings
from app.market_calendar import is_xau_weekend_ms
from app.models import HistoryBar, SpreadAnalysis, SpreadAnalysisPoint
from app.storage import Storage
from app.quote_guard import MAX_REASONABLE_XAU_MID_GAP


INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
}
MAX_ANALYSIS_ABS_SPREAD = MAX_REASONABLE_XAU_MID_GAP


async def build_spread_analysis(
    settings: Settings,
    storage: Storage,
    days: int,
    interval: str,
    threshold: Decimal,
) -> SpreadAnalysis:
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 60 * 60 * 1000
    mt4_bars = storage.get_bars("mt4", settings.mt4_symbol, interval, start_ms, end_ms)
    if not mt4_bars:
        return SpreadAnalysis(
            ready=False,
            reason="MT4 还没有上传这个周期的历史K线",
            days=days,
            interval=interval,
            threshold=threshold,
            mt4_bars=0,
            binance_bars=0,
            matched_points=0,
            returned_to_threshold=False,
            return_count=0,
        )

    binance_bars = await fetch_and_store_binance_klines(settings, storage, interval, start_ms, end_ms)
    return compare_spreads(mt4_bars, binance_bars, days, interval, threshold)


async def fetch_and_store_binance_klines(
    settings: Settings,
    storage: Storage,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[HistoryBar]:
    bars = await fetch_binance_klines(settings, interval, start_ms, end_ms)
    storage.upsert_bars("binance", settings.binance_symbol, interval, bars)
    return bars


def stored_binance_klines(
    settings: Settings,
    storage: Storage,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[HistoryBar]:
    return storage.get_bars("binance", settings.binance_symbol, interval, start_ms, end_ms)


async def fetch_binance_klines(settings: Settings, interval: str, start_ms: int, end_ms: int) -> list[HistoryBar]:
    if interval not in INTERVAL_MS:
        raise ValueError("不支持的K线周期")
    bars: list[HistoryBar] = []
    next_start = start_ms
    async with httpx.AsyncClient(base_url=settings.binance_base_url, timeout=15, trust_env=False) as client:
        while next_start <= end_ms:
            response = await client.get(
                "/fapi/v1/klines",
                params={
                    "symbol": settings.binance_symbol,
                    "interval": interval,
                    "startTime": next_start,
                    "endTime": end_ms,
                    "limit": 1500,
                },
            )
            response.raise_for_status()
            raw_rows: list[list[Any]] = response.json()
            if not raw_rows:
                break
            chunk = [_parse_binance_bar(row) for row in raw_rows]
            bars.extend(chunk)
            last_open = chunk[-1].open_time_ms
            next_start = last_open + INTERVAL_MS[interval]
            if len(raw_rows) < 1500:
                break
    return bars


def compare_spreads(
    mt4_bars: list[HistoryBar],
    binance_bars: list[HistoryBar],
    days: int,
    interval: str,
    threshold: Decimal,
) -> SpreadAnalysis:
    interval_ms = INTERVAL_MS.get(interval)
    if interval_ms is None:
        raise ValueError("不支持的K线周期")
    binance_by_time = {_bucket_time(bar.open_time_ms, interval_ms): bar for bar in binance_bars}
    points: list[SpreadAnalysisPoint] = []
    for mt4_bar in mt4_bars:
        aligned_time = _bucket_time(mt4_bar.open_time_ms, interval_ms)
        binance_bar = binance_by_time.get(aligned_time)
        if not binance_bar:
            continue
        diff = binance_bar.close - mt4_bar.close
        if _weekend_bar(aligned_time) or abs(diff) > MAX_ANALYSIS_ABS_SPREAD:
            continue
        points.append(
            SpreadAnalysisPoint(
                timestamp_ms=aligned_time,
                mt4_close=mt4_bar.close,
                binance_close=binance_bar.close,
                diff=diff,
                abs_diff=abs(diff),
            )
        )

    if not points:
        return SpreadAnalysis(
            ready=False,
            reason="MT4 和 Binance 的K线时间没有对齐",
            days=days,
            interval=interval,
            threshold=threshold,
            mt4_bars=len(mt4_bars),
            binance_bars=len(binance_bars),
            matched_points=0,
            returned_to_threshold=False,
            return_count=0,
        )

    closest = sorted(points, key=lambda point: point.abs_diff)[:10]
    latest = points[-30:]
    min_point = closest[0]
    return_count = sum(1 for point in points if point.abs_diff <= threshold)
    return SpreadAnalysis(
        ready=True,
        days=days,
        interval=interval,
        threshold=threshold,
        mt4_bars=len(mt4_bars),
        binance_bars=len(binance_bars),
        matched_points=len(points),
        returned_to_threshold=return_count > 0,
        return_count=return_count,
        min_abs_diff=min_point.abs_diff,
        min_abs_diff_time_ms=min_point.timestamp_ms,
        latest_diff=points[-1].diff,
        latest_time_ms=points[-1].timestamp_ms,
        closest_points=closest,
        latest_points=latest,
    )


def _parse_binance_bar(row: list[Any]) -> HistoryBar:
    return HistoryBar(
        open_time_ms=int(row[0]),
        open=Decimal(str(row[1])),
        high=Decimal(str(row[2])),
        low=Decimal(str(row[3])),
        close=Decimal(str(row[4])),
        volume=Decimal(str(row[5])),
    )


def _bucket_time(timestamp_ms: int, interval_ms: int) -> int:
    return timestamp_ms - (timestamp_ms % interval_ms)


def _weekend_bar(open_time_ms: int) -> bool:
    return is_xau_weekend_ms(open_time_ms)
