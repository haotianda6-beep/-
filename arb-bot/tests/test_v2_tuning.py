from decimal import Decimal

from app.v2_tuning import _simulate_candidate, build_entry_model


def test_entry_model_selects_threshold_with_target_win_rate():
    values = [
        Decimal("1.0"),
        Decimal("2.5"),
        Decimal("1.2"),
        Decimal("2.6"),
        Decimal("1.1"),
        Decimal("2.7"),
        Decimal("1.0"),
        Decimal("2.8"),
        Decimal("1.2"),
        Decimal("2.9"),
    ]

    model = build_entry_model(
        values=values,
        manual_min=Decimal("2.0"),
        slippage_budget=Decimal("0.3"),
        exit_follow_budget=Decimal("0.2"),
        close_profit=Decimal("0.3"),
        max_hold_minutes=3,
        min_points=8,
    )

    assert model["enabled"] is True
    assert model["suggested_threshold"] is not None
    assert model["selected"]["win_rate"] >= Decimal("0.70")
    assert model["selected"]["trades"] > 0


def test_entry_model_prefers_daily_trade_target_over_high_frequency():
    values = [Decimal("1.0")] * 1440
    for index in range(10, 1430, 20):
        values[index] = Decimal("3.0")
        values[index + 1] = Decimal("1.0")
    for index in (120, 420, 780, 1140):
        values[index] = Decimal("5.0")
        values[index + 1] = Decimal("1.0")

    model = build_entry_model(
        values=values,
        manual_min=Decimal("2.0"),
        slippage_budget=Decimal("0.3"),
        exit_follow_budget=Decimal("0.6"),
        close_profit=Decimal("0.1"),
        max_hold_minutes=60,
        min_points=8,
    )

    assert model["suggested_threshold"] == Decimal("4.7")
    assert model["selected"]["entry_trigger_spread"] == Decimal("5.0")
    assert Decimal("3") <= model["selected"]["projected_daily_trades"] <= Decimal("5")
    assert "3-5单" in model["reason"]


def test_entry_model_excludes_abnormal_threshold_candidates():
    values = [
        Decimal("1.0"),
        Decimal("3.0"),
        Decimal("1.0"),
        Decimal("12.3"),
        Decimal("1.0"),
        Decimal("3.0"),
        Decimal("1.0"),
        Decimal("12.3"),
        Decimal("1.0"),
        Decimal("3.0"),
        Decimal("1.0"),
        Decimal("12.3"),
    ]

    model = build_entry_model(
        values=values,
        manual_min=Decimal("2.0"),
        slippage_budget=Decimal("0.3"),
        exit_follow_budget=Decimal("0.6"),
        close_profit=Decimal("0.1"),
        max_hold_minutes=3,
        min_points=8,
        max_threshold=Decimal("4.00"),
    )

    assert model["suggested_threshold"] <= Decimal("4.00")
    assert model["points"] == 9
    assert all(candidate["threshold"] <= Decimal("4.00") for candidate in model["candidates"])


def test_entry_model_applies_entry_cooldown_to_projected_frequency():
    values = [Decimal("1.0")] * 1440
    for index in range(10, 1430, 20):
        values[index] = Decimal("3.0")
        values[index + 1] = Decimal("1.0")

    model = build_entry_model(
        values=values,
        manual_min=Decimal("2.0"),
        slippage_budget=Decimal("0.3"),
        exit_follow_budget=Decimal("0.6"),
        close_profit=Decimal("0.1"),
        max_hold_minutes=60,
        min_points=8,
        entry_cooldown_minutes=330,
    )

    assert Decimal("3") <= model["selected"]["projected_daily_trades"] <= Decimal("5")
    assert model["selected"]["entry_cooldown_minutes"] == 330
    assert "3-5单" in model["reason"]


def test_entry_model_subtracts_spread_protection_from_exit_target():
    model = build_entry_model(
        values=[Decimal("1.0"), Decimal("3.3"), Decimal("1.0"), Decimal("3.3")] * 4,
        manual_min=Decimal("3.0"),
        slippage_budget=Decimal("0.3"),
        exit_follow_budget=Decimal("0.6"),
        close_profit=Decimal("0.1"),
        max_hold_minutes=3,
        min_points=8,
        spread_protection_budget=Decimal("0.31"),
    )

    assert model["selected"]["target_exit_spread"] == Decimal("1.99")
    assert model["selected"]["entry_trigger_spread"] == Decimal("3.3")
    assert model["selected"]["spread_protection_budget"] == Decimal("0.31")


def test_entry_model_uses_full_profit_before_age_relaxation():
    result = _simulate_candidate(
        values=[Decimal("3.0"), Decimal("1.5"), Decimal("1.4"), Decimal("1.9")],
        threshold=Decimal("3.0"),
        slippage_budget=Decimal("0"),
        exit_follow_budget=Decimal("0.6"),
        close_profit=Decimal("1.1"),
        max_hold_minutes=3,
        spread_protection_budget=Decimal("0.3"),
        aged_close_profit=Decimal("0.1"),
    )

    assert result["initial_target_exit_spread"] == Decimal("1.0")
    assert result["aged_target_exit_spread"] == Decimal("2.0")
    assert result["wins"] == 1
    assert result["losses"] == 0


def test_entry_model_does_not_count_early_reversion_that_only_meets_aged_target():
    result = _simulate_candidate(
        values=[Decimal("3.0"), Decimal("1.5"), Decimal("1.4")],
        threshold=Decimal("3.0"),
        slippage_budget=Decimal("0"),
        exit_follow_budget=Decimal("0.6"),
        close_profit=Decimal("1.1"),
        max_hold_minutes=3,
        spread_protection_budget=Decimal("0.3"),
        aged_close_profit=Decimal("0.1"),
    )

    assert result["initial_target_exit_spread"] == Decimal("1.0")
    assert result["aged_target_exit_spread"] == Decimal("2.0")
    assert result["wins"] == 0
    assert result["losses"] == 1


def test_entry_model_falls_back_when_no_reversion_is_proven():
    values = [Decimal(str(item)) for item in range(1, 11)]

    model = build_entry_model(
        values=values,
        manual_min=Decimal("2.0"),
        slippage_budget=Decimal("0.3"),
        exit_follow_budget=Decimal("0.2"),
        close_profit=Decimal("0.3"),
        max_hold_minutes=3,
        min_points=8,
    )

    assert model["enabled"] is True
    assert model["suggested_threshold"] is None
    assert "沿用区间阈值" in model["reason"]


def test_entry_model_reports_insufficient_samples():
    model = build_entry_model(
        values=[Decimal("2.0")],
        manual_min=Decimal("2.0"),
        slippage_budget=Decimal("0.3"),
        exit_follow_budget=Decimal("0.2"),
        close_profit=Decimal("0.3"),
        max_hold_minutes=3,
        min_points=8,
    )

    assert model["enabled"] is False
    assert model["suggested_threshold"] is None
