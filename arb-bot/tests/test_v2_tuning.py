from decimal import Decimal

from app.v2_tuning import build_entry_model


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

    assert model["suggested_threshold"] == Decimal("5.0")
    assert Decimal("3") <= model["selected"]["projected_daily_trades"] <= Decimal("5")
    assert "3-5单" in model["reason"]


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
