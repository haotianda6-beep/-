from app.mt4_rollover import DAY_MS, normalize_mt4_rollover_ms


def test_normalize_mt4_rollover_keeps_future_time():
    assert normalize_mt4_rollover_ms(2_000, now_ms=1_000) == 2_000


def test_normalize_mt4_rollover_rolls_recent_stale_time_forward():
    assert normalize_mt4_rollover_ms(1_000, now_ms=1_001) == 1_000 + DAY_MS


def test_normalize_mt4_rollover_rejects_very_old_time():
    assert normalize_mt4_rollover_ms(1_000, now_ms=10 * DAY_MS + 1_000) is None
