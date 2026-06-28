from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.core.models import BotSettings, CashCarryOpportunity, DataSource, ExchangeName
from app.services.cash_carry_shadow_memory import CashCarryShadowMemory


def test_cash_carry_shadow_memory_persists_open_samples(tmp_path) -> None:
    state = tmp_path / "state.json"
    now = datetime.now(timezone.utc)
    settings = BotSettings(order_notional_usdt=Decimal("300"))

    memory = CashCarryShadowMemory(state)
    memory.observe([_candidate("PROBEUSDT", Decimal("0.42"), Decimal("0.20"), ["合约溢价未达 0.8%"])], settings, Decimal("0.9"), now)

    restored = CashCarryShadowMemory(state)

    assert restored.summary(now).open_count == 1


def test_cash_carry_shadow_memory_closes_missing_sample_after_timeout(tmp_path) -> None:
    state = tmp_path / "state.json"
    now = datetime.now(timezone.utc)
    settings = BotSettings(order_notional_usdt=Decimal("300"))

    memory = CashCarryShadowMemory(state)
    memory.observe([_candidate("DROPUSDT", Decimal("0.42"), Decimal("0.20"), ["合约溢价未达 0.8%"])], settings, Decimal("0.9"), now)
    memory.observe([], settings, Decimal("0.9"), now + timedelta(hours=4))

    summary = CashCarryShadowMemory(state).summary(now + timedelta(hours=4))

    assert summary.open_count == 0
    assert summary.closed_count == 1
    assert summary.wins == 0
    assert summary.total_estimated_net == Decimal("-0.5")


def test_cash_carry_shadow_memory_summary_can_filter_exchange(tmp_path) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        '{"positions":[],"cash_carry_shadow":{"open":[], "closed":['
        '{"opened_at":"2026-06-28T00:00:00+00:00","exchange":"GATE","symbol":"GUSDT","entry_basis_pct":"0.6","max_basis_pct":"0.7","closed_at":"2026-06-28T00:10:00+00:00","close_basis_pct":"0.1","estimated_net_profit":"1.2","reason":"基差回归"},'
        '{"opened_at":"2026-06-28T00:00:00+00:00","exchange":"BITGET","symbol":"BUSDT","entry_basis_pct":"0.5","max_basis_pct":"0.6","closed_at":"2026-06-28T00:10:00+00:00","close_basis_pct":"0.2","estimated_net_profit":"0.4","reason":"基差回归"}'
        ']}}',
        encoding="utf-8",
    )

    gate = CashCarryShadowMemory(state).summary(exchange=ExchangeName.GATE)
    bitget = CashCarryShadowMemory(state).summary(exchange=ExchangeName.BITGET)

    assert gate.closed_count == 1
    assert gate.total_estimated_net == Decimal("1.2")
    assert gate.min_winning_entry_basis_pct == Decimal("0.6")
    assert bitget.closed_count == 1
    assert bitget.total_estimated_net == Decimal("0.4")


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
