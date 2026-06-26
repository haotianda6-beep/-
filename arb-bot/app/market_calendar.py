from __future__ import annotations

from datetime import datetime, timedelta, timezone


CHINA_TZ = timezone(timedelta(hours=8))


def is_xau_weekend_ms(timestamp_ms: int) -> bool:
    """Treat both UTC weekends and China-calendar weekends as closed samples."""
    return _weekday(timestamp_ms, timezone.utc) >= 5 or _weekday(timestamp_ms, CHINA_TZ) >= 5


def xau_weekend_entry_block_reason(timestamp_ms: int, buffer_minutes: int = 60) -> str | None:
    if is_xau_weekend_ms(timestamp_ms):
        return "黄金处于周末/停盘过滤时段"
    ms_left = min(_ms_until_weekend(timestamp_ms, timezone.utc), _ms_until_weekend(timestamp_ms, CHINA_TZ))
    buffer_ms = max(0, buffer_minutes) * 60_000
    if 0 <= ms_left <= buffer_ms:
        minutes_left = max(1, (ms_left + 59_999) // 60_000)
        return f"距离黄金周末/停盘过滤时段约 {minutes_left} 分钟"
    return None


def _weekday(timestamp_ms: int, tz: timezone) -> int:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz).weekday()


def _ms_until_weekend(timestamp_ms: int, tz: timezone) -> int:
    local = datetime.fromtimestamp(timestamp_ms / 1000, tz)
    if local.weekday() >= 5:
        return 0
    days_until_saturday = 5 - local.weekday()
    weekend_start = (local + timedelta(days=days_until_saturday)).replace(hour=0, minute=0, second=0, microsecond=0)
    current_utc = datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc)
    return max(0, int((weekend_start.astimezone(timezone.utc) - current_utc).total_seconds() * 1000))
