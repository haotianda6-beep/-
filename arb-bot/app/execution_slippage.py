from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.storage import Storage

SLIPPAGE_LOOKBACK_MS = 7 * 24 * 60 * 60 * 1000
MAX_LEARNED_SLIPPAGE_USD_PER_OZ = Decimal("2")


def mt4_close_slippage_budget_usd_per_oz(
    storage: Storage,
    now_ms: int,
    percentile: int = 80,
    limit: int = 200,
    start_ms: int | None = None,
) -> Decimal:
    return _slippage_budget_usd_per_oz(
        storage=storage,
        now_ms=now_ms,
        kinds={"v2_pair_pnl_recorded"},
        field="mt4_close_adverse_slippage",
        percentile=percentile,
        limit=limit,
        start_ms=start_ms,
    )


def mt4_entry_slippage_budget_usd_per_oz(
    storage: Storage,
    now_ms: int,
    percentile: int = 80,
    limit: int = 200,
    start_ms: int | None = None,
) -> Decimal:
    return _slippage_budget_usd_per_oz(
        storage=storage,
        now_ms=now_ms,
        kinds={"v2_mt4_entry_slippage", "v2_mt4_add_slippage"},
        field="mt4_entry_adverse_slippage",
        percentile=percentile,
        limit=limit,
        start_ms=start_ms,
    )


def _slippage_budget_usd_per_oz(
    storage: Storage,
    now_ms: int,
    kinds: set[str],
    field: str,
    percentile: int,
    limit: int,
    start_ms: int | None,
) -> Decimal:
    try:
        lookback_start = now_ms - SLIPPAGE_LOOKBACK_MS
        if start_ms is not None:
            lookback_start = max(lookback_start, start_ms)
        events = storage.get_events(lookback_start, now_ms + 1000, limit=limit)
    except Exception:  # noqa: BLE001
        return Decimal("0")
    values: list[Decimal] = []
    for event in events:
        if event.get("kind") not in kinds:
            continue
        payload = event.get("payload") or {}
        value = _positive_decimal(payload.get(field))
        if value is not None:
            values.append(value)
    if not values:
        return Decimal("0")
    values.sort()
    bounded_percentile = min(max(percentile, 0), 100)
    index = ((len(values) - 1) * bounded_percentile) // 100
    return min(values[index], MAX_LEARNED_SLIPPAGE_USD_PER_OZ)


def _positive_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed > 0 else None
