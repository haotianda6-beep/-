from __future__ import annotations

from datetime import datetime, timedelta, timezone


XAU_WEEKEND_START_WEEKDAY = 4
XAU_WEEKEND_START_HOUR_UTC = 22
XAU_WEEKEND_END_WEEKDAY = 6
XAU_WEEKEND_END_HOUR_UTC = 22


def is_xau_weekend_ms(timestamp_ms: int) -> bool:
    current = datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc)
    return _current_xau_weekend_start(current) <= current < _current_xau_weekend_end(current)


def xau_weekend_entry_block_reason(
    timestamp_ms: int,
    buffer_minutes: int = 60,
    reopen_cooldown_minutes: int = 30,
) -> str | None:
    if is_xau_weekend_ms(timestamp_ms):
        return "黄金处于周末/停盘过滤时段"
    ms_left = _ms_until_xau_weekend_start(timestamp_ms)
    buffer_ms = max(0, buffer_minutes) * 60_000
    if 0 <= ms_left <= buffer_ms:
        minutes_left = max(1, (ms_left + 59_999) // 60_000)
        return f"距离黄金周末/停盘过滤时段约 {minutes_left} 分钟"
    cooldown_ms = max(0, reopen_cooldown_minutes) * 60_000
    if cooldown_ms > 0:
        ms_since_open = _ms_since_xau_weekend_end(timestamp_ms)
        if 0 <= ms_since_open <= cooldown_ms:
            minutes_open = max(0, ms_since_open // 60_000)
            return f"黄金周末/停盘刚恢复约 {minutes_open} 分钟，等待开盘点差稳定"
    return None


def _current_xau_weekend_start(current: datetime) -> datetime:
    week_start = (current - timedelta(days=current.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return week_start + timedelta(days=XAU_WEEKEND_START_WEEKDAY, hours=XAU_WEEKEND_START_HOUR_UTC)


def _current_xau_weekend_end(current: datetime) -> datetime:
    week_start = (current - timedelta(days=current.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    weekend_end = week_start + timedelta(days=XAU_WEEKEND_END_WEEKDAY, hours=XAU_WEEKEND_END_HOUR_UTC)
    if current < _current_xau_weekend_start(current):
        weekend_end -= timedelta(days=7)
    return weekend_end


def _next_xau_weekend_start(current: datetime) -> datetime:
    weekend_start = _current_xau_weekend_start(current)
    if current >= weekend_start:
        weekend_start += timedelta(days=7)
    return weekend_start


def _last_xau_weekend_end(current: datetime) -> datetime:
    weekend_end = _current_xau_weekend_end(current)
    if current < weekend_end:
        weekend_end -= timedelta(days=7)
    return weekend_end


def _ms_until_xau_weekend_start(timestamp_ms: int) -> int:
    current_utc = datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc)
    weekend_start = _next_xau_weekend_start(current_utc)
    return max(0, int((weekend_start - current_utc).total_seconds() * 1000))


def _ms_since_xau_weekend_end(timestamp_ms: int) -> int:
    current_utc = datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc)
    weekend_end = _last_xau_weekend_end(current_utc)
    return max(0, int((current_utc - weekend_end).total_seconds() * 1000))
