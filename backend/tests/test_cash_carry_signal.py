from datetime import datetime, timezone
from decimal import Decimal

from app.core.models import BotSettings, CashCarryOpportunity, DataSource, ExchangeName
from app.services.cash_carry_signal import CashCarrySignalTracker
from app.services.live_market_types import CashCarryScan


def test_cash_carry_signal_blocks_first_ready_tick_until_persistent() -> None:
    tracker = CashCarrySignalTracker()
    settings = _settings(cash_carry_signal_min_seconds=Decimal("10"), cash_carry_signal_min_samples=2)

    first = tracker.apply(CashCarryScan(opportunities=[_candidate("ABCUSDT", Decimal("1.2"))]), settings, now=100.0)
    second = tracker.apply(CashCarryScan(candidates=[first.candidates[0].model_copy(update={"blocked_reasons": []})]), settings, now=111.0)

    assert first.opportunities == []
    assert "信号持续不足" in " / ".join(first.candidates[0].blocked_reasons)
    assert second.opportunities
    assert second.opportunities[0].symbol == "ABCUSDT"


def test_cash_carry_signal_blocks_unstable_basis() -> None:
    tracker = CashCarrySignalTracker()
    settings = _settings(
        cash_carry_signal_min_seconds=Decimal("10"),
        cash_carry_signal_min_samples=2,
        cash_carry_signal_max_basis_swing_pct=Decimal("0.2"),
    )

    tracker.apply(CashCarryScan(opportunities=[_candidate("ABCUSDT", Decimal("1.2"))]), settings, now=100.0)
    result = tracker.apply(CashCarryScan(opportunities=[_candidate("ABCUSDT", Decimal("1.6"))]), settings, now=111.0)

    assert result.opportunities == []
    assert "基差波动过大" in " / ".join(result.candidates[0].blocked_reasons)


def test_cash_carry_signal_resets_when_base_quality_fails() -> None:
    tracker = CashCarrySignalTracker()
    settings = _settings(cash_carry_signal_min_seconds=Decimal("10"), cash_carry_signal_min_samples=2)

    tracker.apply(CashCarryScan(opportunities=[_candidate("ABCUSDT", Decimal("1.2"))]), settings, now=100.0)
    tracker.apply(CashCarryScan(candidates=[_candidate("ABCUSDT", Decimal("1.2"), ["资金费率不是正数，空头不能收资金费"])]), settings, now=105.0)
    result = tracker.apply(CashCarryScan(opportunities=[_candidate("ABCUSDT", Decimal("1.2"))]), settings, now=116.0)

    assert result.opportunities == []
    assert "信号持续不足" in " / ".join(result.candidates[0].blocked_reasons)


def test_cash_carry_signal_tolerates_short_quality_gap() -> None:
    tracker = CashCarrySignalTracker()
    settings = _settings(cash_carry_signal_min_seconds=Decimal("10"), cash_carry_signal_min_samples=3)

    tracker.apply(CashCarryScan(opportunities=[_candidate("ABCUSDT", Decimal("1.2"))]), settings, now=100.0)
    tracker.apply(CashCarryScan(opportunities=[_candidate("ABCUSDT", Decimal("1.2"))]), settings, now=103.0)
    tracker.apply(CashCarryScan(candidates=[_candidate("ABCUSDT", Decimal("1.2"), ["资金费率不是正数，空头不能收资金费"])]), settings, now=104.0)
    tracker.apply(CashCarryScan(opportunities=[_candidate("ABCUSDT", Decimal("1.2"))]), settings, now=105.0)
    tracker.apply(CashCarryScan(opportunities=[_candidate("ABCUSDT", Decimal("1.2"))]), settings, now=108.0)
    result = tracker.apply(CashCarryScan(opportunities=[_candidate("ABCUSDT", Decimal("1.2"))]), settings, now=111.0)

    assert result.opportunities
    assert result.opportunities[0].symbol == "ABCUSDT"


def test_cash_carry_signal_keeps_v2_gate_but_requires_stability_before_probe() -> None:
    tracker = CashCarrySignalTracker()
    settings = _settings(
        cash_carry_signal_min_seconds=Decimal("10"),
        cash_carry_signal_min_samples=2,
        cash_carry_signal_min_history_samples=2,
        cash_carry_signal_min_basis_percentile=Decimal("50"),
    )
    reasons = ["V3历史胜率保护：净利预估 3.0000U < 动态安全垫 6.0000U"]

    first = tracker.apply(CashCarryScan(candidates=[_candidate("ABCUSDT", Decimal("1.2"), reasons)]), settings, now=100.0)
    second = tracker.apply(CashCarryScan(candidates=[_candidate("ABCUSDT", Decimal("1.2"), reasons)]), settings, now=111.0)

    assert "V3历史胜率保护" in " / ".join(first.candidates[0].blocked_reasons)
    assert "信号持续不足" in " / ".join(first.candidates[0].blocked_reasons)
    assert second.opportunities == []
    assert second.candidates[0].blocked_reasons == reasons


def test_cash_carry_signal_blocks_low_basis_percentile() -> None:
    tracker = CashCarrySignalTracker()
    settings = _settings(
        cash_carry_signal_min_seconds=Decimal("0"),
        cash_carry_signal_min_samples=1,
        cash_carry_signal_max_basis_swing_pct=Decimal("0"),
        cash_carry_signal_min_history_samples=4,
        cash_carry_signal_min_basis_percentile=Decimal("75"),
    )

    for offset, basis in enumerate([Decimal("3"), Decimal("4"), Decimal("5"), Decimal("2")]):
        result = tracker.apply(CashCarryScan(opportunities=[_candidate("ABCUSDT", basis)]), settings, now=100.0 + offset)

    assert result.opportunities == []
    assert "基差分位不足" in " / ".join(result.candidates[0].blocked_reasons)


def test_cash_carry_signal_shortens_wait_when_net_cushion_is_high() -> None:
    tracker = CashCarrySignalTracker()
    settings = _settings(
        order_notional_usdt=Decimal("300"),
        cash_carry_signal_min_seconds=Decimal("20"),
        cash_carry_signal_min_samples=3,
    )

    for offset in [0, 3, 6, 9, 11]:
        result = tracker.apply(CashCarryScan(opportunities=[_candidate("ABCUSDT", Decimal("1.2"), net=Decimal("1.1"))]), settings, now=100.0 + offset)

    assert result.opportunities
    assert result.opportunities[0].symbol == "ABCUSDT"


def test_cash_carry_signal_keeps_full_wait_when_net_cushion_is_thin() -> None:
    tracker = CashCarrySignalTracker()
    settings = _settings(
        order_notional_usdt=Decimal("300"),
        cash_carry_signal_min_seconds=Decimal("20"),
        cash_carry_signal_min_samples=3,
    )

    for offset in [0, 3, 6, 9, 11]:
        result = tracker.apply(CashCarryScan(opportunities=[_candidate("ABCUSDT", Decimal("1.2"), net=Decimal("0.9"))]), settings, now=100.0 + offset)

    assert result.opportunities == []
    assert "信号持续不足" in " / ".join(result.candidates[0].blocked_reasons)


def test_cash_carry_signal_relaxes_percentile_when_net_cushion_is_high() -> None:
    tracker = CashCarrySignalTracker()
    settings = _settings(
        order_notional_usdt=Decimal("300"),
        cash_carry_signal_min_seconds=Decimal("0"),
        cash_carry_signal_min_samples=1,
        cash_carry_signal_max_basis_swing_pct=Decimal("0"),
        cash_carry_signal_min_history_samples=10,
        cash_carry_signal_min_basis_percentile=Decimal("75"),
    )

    for offset, basis in enumerate([Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4"), Decimal("5"), Decimal("6"), Decimal("8"), Decimal("9"), Decimal("10"), Decimal("6")]):
        result = tracker.apply(CashCarryScan(opportunities=[_candidate("ABCUSDT", basis, net=Decimal("1.1"))]), settings, now=100.0 + offset)

    assert result.opportunities


def test_cash_carry_signal_keeps_percentile_when_net_cushion_is_thin() -> None:
    tracker = CashCarrySignalTracker()
    settings = _settings(
        order_notional_usdt=Decimal("300"),
        cash_carry_signal_min_seconds=Decimal("0"),
        cash_carry_signal_min_samples=1,
        cash_carry_signal_max_basis_swing_pct=Decimal("0"),
        cash_carry_signal_min_history_samples=10,
        cash_carry_signal_min_basis_percentile=Decimal("75"),
    )

    for offset, basis in enumerate([Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4"), Decimal("5"), Decimal("6"), Decimal("8"), Decimal("9"), Decimal("10"), Decimal("6")]):
        result = tracker.apply(CashCarryScan(opportunities=[_candidate("ABCUSDT", basis, net=Decimal("0.9"))]), settings, now=100.0 + offset)

    assert result.opportunities == []
    assert "基差分位不足 70.00% < 75%" in " / ".join(result.candidates[0].blocked_reasons)


def test_cash_carry_signal_allows_high_basis_percentile() -> None:
    tracker = CashCarrySignalTracker()
    settings = _settings(
        cash_carry_signal_min_seconds=Decimal("0"),
        cash_carry_signal_min_samples=1,
        cash_carry_signal_max_basis_swing_pct=Decimal("0"),
        cash_carry_signal_min_history_samples=4,
        cash_carry_signal_min_basis_percentile=Decimal("75"),
    )

    for offset, basis in enumerate([Decimal("2"), Decimal("3"), Decimal("4"), Decimal("5")]):
        result = tracker.apply(CashCarryScan(opportunities=[_candidate("ABCUSDT", basis)]), settings, now=100.0 + offset)

    assert result.opportunities
    assert result.opportunities[0].symbol == "ABCUSDT"


def _settings(**overrides) -> BotSettings:
    defaults = {
        "cash_carry_signal_min_history_samples": 1,
        "cash_carry_signal_min_basis_percentile": Decimal("0"),
    }
    return BotSettings(**{**defaults, **overrides})


def _candidate(symbol: str, basis: Decimal, reasons: list[str] | None = None, net: Decimal = Decimal("3")) -> CashCarryOpportunity:
    return CashCarryOpportunity(
        exchange=ExchangeName.GATE,
        symbol=symbol,
        spot_price=Decimal("100"),
        perp_price=Decimal("101"),
        basis_pct=basis,
        funding_rate_pct=Decimal("0.01"),
        quantity=Decimal("3"),
        spot_volume_24h_usdt=Decimal("1000000"),
        perp_volume_24h_usdt=Decimal("1000000"),
        estimated_basis_profit=Decimal("3"),
        estimated_funding_income=Decimal("0.03"),
        estimated_open_close_fee=Decimal("0.5"),
        estimated_net_profit=net,
        notional_usdt=Decimal("300"),
        margin_required_usdt=Decimal("100"),
        leverage=Decimal("3"),
        blocked_reasons=reasons or [],
        data_source=DataSource.LIVE,
        updated_at=datetime.now(timezone.utc),
    )
