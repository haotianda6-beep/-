from __future__ import annotations

from datetime import datetime, timedelta, timezone


CHINA_TZ = timezone(timedelta(hours=8))


def is_xau_weekend_ms(timestamp_ms: int) -> bool:
    """Treat both UTC weekends and China-calendar weekends as closed samples."""
    return _weekday(timestamp_ms, timezone.utc) >= 5 or _weekday(timestamp_ms, CHINA_TZ) >= 5


def _weekday(timestamp_ms: int, tz: timezone) -> int:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz).weekday()
