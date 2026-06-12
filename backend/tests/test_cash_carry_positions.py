from datetime import datetime, timezone
from decimal import Decimal

from app.core.models import BotSettings, CashCarryOpportunity, DataSource, ExchangeName, PositionSnapshot
from app.services.cash_carry_positions import CashCarryPositionBuilder


def test_cash_carry_position_uses_executable_close_bid_ask_for_basis() -> None:
    builder = _Builder(_TickerCache({
        ("spot", "AIAUSDT"): {"bid": "99", "ask": "100"},
        ("swap", "AIAUSDT"): {"bid": "99.1", "ask": "100"},
    }))
    row = builder.build([_position()], [_opportunity()], BotSettings())[0]

    assert row.spot_price == Decimal("99.0000")
    assert row.perp_mark_price == Decimal("100.0000")
    assert row.basis_pct == Decimal("1.0101")
    assert row.current_net_profit < 0


def test_cash_carry_position_shows_state_only_spot_leg() -> None:
    builder = _Builder(_TickerCache({
        ("spot", "AIAUSDT"): {"bid": "99", "ask": "100"},
        ("swap", "AIAUSDT"): {"bid": "99.1", "ask": "100"},
    }))

    row = builder.build([], [_opportunity()], BotSettings())[0]

    assert row.exchange == ExchangeName.GATE
    assert row.symbol == "AIAUSDT"
    assert row.status == "spot_only"
    assert row.perp_side == "none"
    assert row.spot_quantity == Decimal("1.000000")
    assert row.perp_base_quantity == Decimal("0")
    assert row.quantity_gap == Decimal("1.000000")


class _TickerCache:
    def __init__(self, tickers):
        self.tickers = tickers

    def subscribe(self, exchange, market_type, symbol, ccxt_symbol):
        return None

    def get(self, exchange, market_type, symbol):
        return self.tickers.get((market_type, symbol))


class _Builder(CashCarryPositionBuilder):
    def _cached_spot_quantity(self, exchange, base):
        return Decimal("1")

    def _cached_contract_size(self, exchange, symbol):
        return Decimal("1")

    def _state_records(self):
        return {
            (ExchangeName.GATE, "AIAUSDT"): {
                "spot_entry_price": "100",
                "perp_entry_price": "101",
                "add_count": 0,
            }
        }


def _position() -> PositionSnapshot:
    return PositionSnapshot(
        exchange=ExchangeName.GATE,
        symbol="AIAUSDT",
        side="short",
        quantity=Decimal("1"),
        entry_price=Decimal("101"),
        mark_price=Decimal("99.1"),
        leverage=Decimal("2"),
        unrealized_pnl=Decimal("1.9"),
    )


def _opportunity() -> CashCarryOpportunity:
    return CashCarryOpportunity(
        exchange=ExchangeName.GATE,
        symbol="AIAUSDT",
        spot_price=Decimal("100"),
        perp_price=Decimal("99.1"),
        basis_pct=Decimal("-0.9"),
        funding_rate_pct=Decimal("0.01"),
        quantity=Decimal("1"),
        spot_volume_24h_usdt=Decimal("1000000"),
        perp_volume_24h_usdt=Decimal("1000000"),
        estimated_basis_profit=Decimal("0"),
        estimated_funding_income=Decimal("0"),
        estimated_open_close_fee=Decimal("0.1"),
        estimated_net_profit=Decimal("0"),
        blocked_reasons=[],
        data_source=DataSource.LIVE,
        updated_at=datetime.now(timezone.utc),
    )
