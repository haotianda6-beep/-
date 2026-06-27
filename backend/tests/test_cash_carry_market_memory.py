from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.core.models import CashCarryOpportunity, DataSource, ExchangeName
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
