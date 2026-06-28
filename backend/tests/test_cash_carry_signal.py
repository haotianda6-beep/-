from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.core.models import BotSettings, CashCarryOpportunity, DataSource, ExchangeName
from app.services.cash_carry_history_quality import CashCarryHistoryQuality
from app.services.cash_carry_signal import CashCarrySignalTracker
from app.services.live_market_types import CashCarryScan


EMPTY_HISTORY = Path(__file__).with_name("missing_cash_carry_history.json")


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


def test_cash_carry_signal_fast_captures_thick_net_after_two_ticks() -> None:
    tracker = CashCarrySignalTracker()
    settings = _settings(
        order_notional_usdt=Decimal("300"),
        cash_carry_min_basis_pct=Decimal("0.8"),
        cash_carry_signal_min_seconds=Decimal("20"),
        cash_carry_signal_min_samples=5,
        cash_carry_signal_min_history_samples=30,
        cash_carry_signal_min_basis_percentile=Decimal("75"),
    )
    soft_reason = ["V3冷启动净利预估 0.1000U < 冷启动安全垫 0.9000U"]
    for offset in range(6):
        tracker.apply(CashCarryScan(candidates=[_candidate("ABCUSDT", Decimal("0.3"), soft_reason, net=Decimal("0.1"))]), settings, now=100.0 + offset)

    tracker.apply(CashCarryScan(opportunities=[_candidate("ABCUSDT", Decimal("0.9"), net=Decimal("1.6"))]), settings, now=106.0)
    result = tracker.apply(CashCarryScan(opportunities=[_candidate("ABCUSDT", Decimal("0.9"), net=Decimal("1.6"))]), settings, now=108.0)

    assert result.opportunities
    assert result.opportunities[0].symbol == "ABCUSDT"


def test_cash_carry_signal_fast_capture_requires_entry_basis() -> None:
    tracker = CashCarrySignalTracker()
    settings = _settings(
        order_notional_usdt=Decimal("300"),
        cash_carry_min_basis_pct=Decimal("0.8"),
        cash_carry_bootstrap_enabled=False,
        cash_carry_signal_min_seconds=Decimal("20"),
        cash_carry_signal_min_samples=5,
    )

    for offset in [0, 2]:
        result = tracker.apply(CashCarryScan(opportunities=[_candidate("ABCUSDT", Decimal("0.7"), net=Decimal("1.8"))]), settings, now=100.0 + offset)

    assert result.opportunities == []
    assert "信号持续不足" in " / ".join(result.candidates[0].blocked_reasons)


def test_cash_carry_signal_fast_captures_bootstrap_basis_when_net_covers_floor() -> None:
    tracker = CashCarrySignalTracker(CashCarryHistoryQuality(EMPTY_HISTORY))
    settings = _settings(
        order_notional_usdt=Decimal("300"),
        cash_carry_min_basis_pct=Decimal("0.8"),
        cash_carry_bootstrap_enabled=True,
        cash_carry_bootstrap_min_basis_pct=Decimal("0.6"),
        cash_carry_bootstrap_min_trades=3,
        cash_carry_signal_min_seconds=Decimal("20"),
        cash_carry_signal_min_samples=5,
        cash_carry_signal_min_history_samples=30,
        cash_carry_signal_min_basis_percentile=Decimal("75"),
    )
    soft_reason = ["V3冷启动净利预估 0.1000U < 冷启动安全垫 0.9000U"]
    for offset in range(6):
        tracker.apply(CashCarryScan(candidates=[_candidate("ABCUSDT", Decimal("0.3"), soft_reason, net=Decimal("0.1"))]), settings, now=100.0 + offset)

    tracker.apply(CashCarryScan(opportunities=[_candidate("ABCUSDT", Decimal("0.68"), net=Decimal("1.0"))]), settings, now=106.0)
    result = tracker.apply(CashCarryScan(opportunities=[_candidate("ABCUSDT", Decimal("0.68"), net=Decimal("1.0"))]), settings, now=108.0)

    assert result.opportunities
    assert result.opportunities[0].symbol == "ABCUSDT"


def test_cash_carry_signal_burst_captures_single_thick_tick() -> None:
    tracker = CashCarrySignalTracker(CashCarryHistoryQuality(EMPTY_HISTORY))
    settings = _settings(
        order_notional_usdt=Decimal("300"),
        cash_carry_min_basis_pct=Decimal("0.8"),
        cash_carry_bootstrap_enabled=False,
        cash_carry_signal_min_seconds=Decimal("20"),
        cash_carry_signal_min_samples=5,
        cash_carry_signal_min_history_samples=30,
        cash_carry_signal_min_basis_percentile=Decimal("75"),
    )

    result = tracker.apply(CashCarryScan(opportunities=[_candidate("ABCUSDT", Decimal("1.10"), net=Decimal("2.5"))]), settings, now=100.0)

    assert result.opportunities
    assert result.opportunities[0].symbol == "ABCUSDT"


def test_cash_carry_signal_burst_capture_respects_explicit_depth_limit() -> None:
    tracker = CashCarrySignalTracker(CashCarryHistoryQuality(EMPTY_HISTORY))
    settings = _settings(
        order_notional_usdt=Decimal("300"),
        cash_carry_min_basis_pct=Decimal("0.8"),
        cash_carry_bootstrap_enabled=False,
        cash_carry_signal_min_seconds=Decimal("20"),
        cash_carry_signal_min_samples=5,
        cash_carry_signal_min_history_samples=30,
        cash_carry_signal_min_basis_percentile=Decimal("75"),
    )

    result = tracker.apply(
        CashCarryScan(opportunities=[_candidate("ABCUSDT", Decimal("1.10"), net=Decimal("2.5"), max_safe=Decimal("100"))]),
        settings,
        now=100.0,
    )

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


def test_cash_carry_signal_relaxes_history_samples_when_net_cushion_is_high() -> None:
    tracker = CashCarrySignalTracker()
    settings = _settings(
        order_notional_usdt=Decimal("300"),
        cash_carry_signal_min_seconds=Decimal("0"),
        cash_carry_signal_min_samples=1,
        cash_carry_signal_max_basis_swing_pct=Decimal("0"),
        cash_carry_signal_min_history_samples=30,
        cash_carry_signal_min_basis_percentile=Decimal("75"),
    )

    for offset in range(20):
        result = tracker.apply(CashCarryScan(opportunities=[_candidate("ABCUSDT", Decimal(offset + 1), net=Decimal("1.1"))]), settings, now=100.0 + offset)

    assert result.opportunities
    assert result.opportunities[0].symbol == "ABCUSDT"


def test_cash_carry_signal_keeps_full_history_samples_when_net_cushion_is_thin() -> None:
    tracker = CashCarrySignalTracker()
    settings = _settings(
        order_notional_usdt=Decimal("300"),
        cash_carry_signal_min_seconds=Decimal("0"),
        cash_carry_signal_min_samples=1,
        cash_carry_signal_max_basis_swing_pct=Decimal("0"),
        cash_carry_signal_min_history_samples=30,
        cash_carry_signal_min_basis_percentile=Decimal("75"),
    )

    for offset in range(20):
        result = tracker.apply(CashCarryScan(opportunities=[_candidate("ABCUSDT", Decimal(offset + 1), net=Decimal("0.9"))]), settings, now=100.0 + offset)

    assert result.opportunities == []
    assert "基差分位样本不足 20/30" in " / ".join(result.candidates[0].blocked_reasons)


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


def _candidate(
    symbol: str,
    basis: Decimal,
    reasons: list[str] | None = None,
    net: Decimal = Decimal("3"),
    max_safe: Decimal | None = None,
) -> CashCarryOpportunity:
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
        max_safe_notional_usdt=max_safe,
        notional_usdt=Decimal("300"),
        margin_required_usdt=Decimal("100"),
        leverage=Decimal("3"),
        blocked_reasons=reasons or [],
        data_source=DataSource.LIVE,
        updated_at=datetime.now(timezone.utc),
    )
