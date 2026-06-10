from datetime import datetime, timezone
from decimal import Decimal

from app.core.models import BotSettings, CashCarryOpportunity, DataSource, ExchangeName
from app.services.cash_carry_executor import CashCarryExecutor


def test_bitget_transfer_uses_swap_to_spot_when_spot_usdt_is_short(tmp_path) -> None:
    executor = CashCarryExecutor(tmp_path / "state.json")
    exchange = _FakeBitget(spot_free=Decimal("1"))
    settings = BotSettings(order_notional_usdt=Decimal("100"), default_leverage=Decimal("5"), cash_carry_auto_transfer_enabled=True)
    step = executor._open_plan(_opportunity(), settings)[0]

    executor._maybe_transfer(exchange, _opportunity(), settings, step)

    assert step.status == "done"
    assert exchange.transfers == [("USDT", 100.0, "swap", "spot")]


def _opportunity() -> CashCarryOpportunity:
    return CashCarryOpportunity(
        exchange=ExchangeName.BITGET,
        symbol="ABCUSDT",
        spot_price=Decimal("100"),
        perp_price=Decimal("101"),
        basis_pct=Decimal("1"),
        funding_rate_pct=Decimal("0.01"),
        quantity=Decimal("1"),
        spot_volume_24h_usdt=Decimal("1000000"),
        perp_volume_24h_usdt=Decimal("1000000"),
        estimated_basis_profit=Decimal("1"),
        estimated_funding_income=Decimal("0.01"),
        estimated_open_close_fee=Decimal("0.2"),
        estimated_net_profit=Decimal("0.8"),
        blocked_reasons=[],
        data_source=DataSource.LIVE,
        updated_at=datetime.now(timezone.utc),
    )


class _FakeBitget:
    id = "bitget"

    def __init__(self, spot_free: Decimal) -> None:
        self.spot_free = spot_free
        self.transfers = []

    def fetch_balance(self, params):
        return {"USDT": {"free": str(self.spot_free)}}

    def transfer(self, code, amount, from_account, to_account):
        self.transfers.append((code, amount, from_account, to_account))
        return {"ok": True}
