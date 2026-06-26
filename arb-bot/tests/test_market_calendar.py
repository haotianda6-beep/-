from datetime import datetime, timezone

from app.market_calendar import is_xau_weekend_ms, xau_weekend_entry_block_reason


def ms(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp() * 1000)


def test_xau_weekend_entry_block_uses_friday_utc_close_boundary():
    reason = xau_weekend_entry_block_reason(ms(2026, 6, 26, 21, 30), buffer_minutes=60)

    assert reason is not None
    assert "周末" in reason


def test_xau_weekend_entry_block_allows_friday_before_buffer():
    assert xau_weekend_entry_block_reason(ms(2026, 6, 26, 20, 0), buffer_minutes=60) is None


def test_xau_weekend_allows_china_saturday_early_when_utc_friday_open():
    timestamp = ms(2026, 6, 26, 16, 1)

    assert is_xau_weekend_ms(timestamp) is False
    assert xau_weekend_entry_block_reason(timestamp) is None


def test_xau_weekend_detects_saturday_utc_close():
    timestamp = ms(2026, 6, 27, 12, 0)

    assert is_xau_weekend_ms(timestamp) is True
    assert xau_weekend_entry_block_reason(timestamp) == "黄金处于周末/停盘过滤时段"


def test_xau_weekend_entry_block_waits_after_utc_weekend_reopens():
    reason = xau_weekend_entry_block_reason(ms(2026, 6, 28, 22, 10), reopen_cooldown_minutes=30)

    assert reason is not None
    assert "刚恢复" in reason


def test_xau_weekend_entry_block_allows_after_reopen_cooldown():
    assert xau_weekend_entry_block_reason(ms(2026, 6, 28, 22, 40), reopen_cooldown_minutes=30) is None
