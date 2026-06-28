from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.core.models import BotSettings, CashCarryOpportunity, DataSource, ExchangeName
from app.services.cash_carry_market_memory import CashCarryMarketMemory


def test_cash_carry_market_memory_keeps_best_recent_sample() -> None:
    memory = CashCarryMarketMemory()
    now = datetime.now(timezone.utc)

    memory.observe([_candidate("OLDUSDT", Decimal("1"), Decimal("1"))], now - timedelta(minutes=31))
    memory.observe([_candidate("GOODUSDT", Decimal("1.8"), Decimal("4.2"), ["V3历史胜率保护：净利不足"])], now - timedelta(minutes=5))
    memory.observe([_candidate("LOWUSDT", Decimal("0.4"), Decimal("-1"), ["合约溢价未达 0.8%"])], now)

    summary = memory.summary(Decimal("6"), now)

    assert summary.observations == 2
    assert summary.symbols == 2
    assert summary.best is not None
    assert summary.best.symbol == "GOODUSDT"
    assert summary.base_quality_count == 1
    assert summary.near_count == 0


def test_cash_carry_market_memory_counts_near_floor_samples() -> None:
    memory = CashCarryMarketMemory()
    now = datetime.now(timezone.utc)
    memory.observe(
        [
            _candidate("AUSDT", Decimal("2"), Decimal("5"), ["V3历史胜率保护：净利不足"]),
            _candidate("BUSDT", Decimal("1.5"), Decimal("2"), ["V3历史胜率保护：净利不足"]),
        ],
        now,
    )

    summary = memory.summary(Decimal("6"), now)

    assert summary.near_count == 1
    assert summary.base_quality_count == 2


def test_cash_carry_market_memory_shadow_trade_wins_on_convergence() -> None:
    memory = CashCarryMarketMemory()
    now = datetime.now(timezone.utc)
    settings = BotSettings(order_notional_usdt=Decimal("300"), cash_carry_close_basis_pct=Decimal("0.05"))

    memory.observe_shadow([_candidate("WINUSDT", Decimal("1.00"), Decimal("2.5"), ["信号持续不足"])], settings, Decimal("1"), now)
    memory.observe_shadow([_candidate("WINUSDT", Decimal("0.03"), Decimal("0"), ["合约溢价未达 0.8%"])], settings, Decimal("1"), now + timedelta(minutes=5))

    summary = memory.shadow_summary(now + timedelta(minutes=5))

    assert summary.open_count == 0
    assert summary.closed_count == 1
    assert summary.wins == 1
    assert summary.total_estimated_net == Decimal("1.8100")
    assert summary.avg_estimated_net == Decimal("1.8100")
    assert summary.min_winning_entry_basis_pct == Decimal("1.00")


def test_cash_carry_market_memory_shadow_trade_records_timeout_loss() -> None:
    memory = CashCarryMarketMemory()
    now = datetime.now(timezone.utc)
    settings = BotSettings(order_notional_usdt=Decimal("300"), cash_carry_close_basis_pct=Decimal("0.05"))

    memory.observe_shadow([_candidate("LOSSUSDT", Decimal("1.00"), Decimal("2.5"), ["信号持续不足"])], settings, Decimal("1"), now)
    memory.observe_shadow([_candidate("LOSSUSDT", Decimal("1.40"), Decimal("-1"), ["信号持续不足"])], settings, Decimal("1"), now + timedelta(hours=4))

    summary = memory.shadow_summary(now + timedelta(hours=4))

    assert summary.open_count == 0
    assert summary.closed_count == 1
    assert summary.wins == 0
    assert summary.total_estimated_net == Decimal("-2.3000")
    assert summary.min_winning_entry_basis_pct is None


def test_cash_carry_market_memory_shadow_probe_records_low_positive_net() -> None:
    memory = CashCarryMarketMemory()
    now = datetime.now(timezone.utc)
    settings = BotSettings(order_notional_usdt=Decimal("300"))

    memory.observe_shadow(
        [_candidate("PROBEUSDT", Decimal("0.42"), Decimal("0.20"), ["合约溢价未达 0.8%", "V3冷启动净利预估 0.2000U < 冷启动安全垫 0.9000U"])],
        settings,
        Decimal("0.9"),
        now,
    )

    assert memory.shadow_summary(now).open_count == 1


def test_cash_carry_market_memory_shadow_probe_ignores_hard_blockers() -> None:
    memory = CashCarryMarketMemory()
    now = datetime.now(timezone.utc)
    settings = BotSettings(order_notional_usdt=Decimal("300"))

    memory.observe_shadow(
        [_candidate("BADUSDT", Decimal("1.2"), Decimal("2"), ["资金费率不是正数，空头不能收资金费"])],
        settings,
        Decimal("0.9"),
        now,
    )

    assert memory.shadow_summary(now).open_count == 0


def _candidate(symbol: str, basis: Decimal, net: Decimal, reasons: list[str] | None = None) -> CashCarryOpportunity:
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
