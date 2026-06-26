from datetime import datetime, timezone

from app.market_calendar import is_xau_weekend_ms, xau_weekend_entry_block_reason


def ms(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp() * 1000)


def test_xau_weekend_entry_block_uses_china_weekend_boundary():
    reason = xau_weekend_entry_block_reason(ms(2026, 6, 26, 15, 30), buffer_minutes=60)

    assert reason is not None
    assert "周末" in reason


def test_xau_weekend_entry_block_allows_before_buffer():
    assert xau_weekend_entry_block_reason(ms(2026, 6, 26, 14, 0), buffer_minutes=60) is None


def test_xau_weekend_detects_china_saturday():
    timestamp = ms(2026, 6, 26, 16, 1)

    assert is_xau_weekend_ms(timestamp) is True
    assert xau_weekend_entry_block_reason(timestamp) == "黄金处于周末/停盘过滤时段"
