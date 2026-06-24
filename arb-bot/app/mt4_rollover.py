from __future__ import annotations

from app.models import utc_now_ms


DAY_MS = 24 * 60 * 60 * 1000
MAX_STALE_ROLLOVER_MS = 7 * DAY_MS


def normalize_mt4_rollover_ms(next_rollover_time_ms: int | None, now_ms: int | None = None) -> int | None:
    if next_rollover_time_ms is None:
        return None
    now = utc_now_ms() if now_ms is None else now_ms
    if next_rollover_time_ms > now:
        return next_rollover_time_ms
    if now - next_rollover_time_ms > MAX_STALE_ROLLOVER_MS:
        return None
    days_ahead = ((now - next_rollover_time_ms) // DAY_MS) + 1
    return next_rollover_time_ms + days_ahead * DAY_MS
