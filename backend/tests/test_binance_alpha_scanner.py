from datetime import datetime, timezone
from decimal import Decimal

from app.core.models import BotSettings
from app.services.binance_alpha_scanner import AlphaToken, BinanceAlphaScanner


def test_alpha_alert_opportunity_when_basis_and_funding_positive():
    scanner = BinanceAlphaScanner()
    settings = BotSettings(
        alpha_alert_notional_usdt=Decimal("100"),
        alpha_alert_min_basis_pct=Decimal("0.8"),
        alpha_alert_min_funding_rate_pct=Decimal("0"),
        alpha_alert_min_volume_usdt=Decimal("1000"),
        alpha_alert_fee_reserve_pct=Decimal("0.2"),
    )

    rows = scanner._build_rows(
        [
            AlphaToken(
                base="TEST",
                symbol="TEST",
                alpha_id="ALPHA_1",
                alpha_trade_symbol="ALPHA_1USDT",
                name="Test Token",
                chain_name="BSC",
                contract_address="0xabc",
                price=Decimal("1"),
                volume_24h=Decimal("5000"),
                offline=False,
                fully_delisted=False,
                offsell=False,
                duplicate=False,
            )
        ],
        {"TEST": {"symbol": "TESTUSDT", "bid": Decimal("1.02"), "ask": Decimal("1.021"), "volume": Decimal("6000"), "funding_rate": Decimal("0.01")}},
        settings,
    )

    assert len(rows) == 1
    assert rows[0].blocked_reasons == []
    assert rows[0].basis_pct == Decimal("2.00")
    assert rows[0].estimated_net_profit == Decimal("1.8100")


def test_alpha_alert_duplicate_symbol_is_blocked_for_manual_identity_check():
    scanner = BinanceAlphaScanner()
    settings = BotSettings(alpha_alert_min_volume_usdt=Decimal("1000"))

    row = scanner._row(
        AlphaToken(
            base="TEST",
            symbol="TEST",
            alpha_id="ALPHA_1",
            alpha_trade_symbol="ALPHA_1USDT",
            name="Test Token",
            chain_name="BSC",
            contract_address="0xabc",
            price=Decimal("1"),
            volume_24h=Decimal("5000"),
            offline=False,
            fully_delisted=False,
            offsell=False,
            duplicate=True,
        ),
        {"symbol": "TESTUSDT", "bid": Decimal("1.02"), "ask": Decimal("1.021"), "volume": Decimal("6000"), "funding_rate": Decimal("0.01")},
        settings,
        datetime.now(timezone.utc),
    )

    assert row is not None
    assert "Alpha 同名多链/多个 AlphaId，需人工确认不是同名不同币" in row.blocked_reasons


def test_alpha_alert_extreme_basis_is_filtered_as_bad_symbol_match():
    scanner = BinanceAlphaScanner()
    settings = BotSettings(alpha_alert_min_volume_usdt=Decimal("1000"))

    row = scanner._row(
        AlphaToken(
            base="SLX",
            symbol="SLX",
            alpha_id="ALPHA_417",
            alpha_trade_symbol="ALPHA_417USDT",
            name="Slimex",
            chain_name="BSC",
            contract_address="0xabc",
            price=Decimal("0.00118"),
            volume_24h=Decimal("5000"),
            offline=False,
            fully_delisted=False,
            offsell=False,
            duplicate=True,
        ),
        {"symbol": "SLXUSDT", "bid": Decimal("0.27232"), "ask": Decimal("0.27234"), "volume": Decimal("6000"), "funding_rate": Decimal("0.01")},
        settings,
        datetime.now(timezone.utc),
    )

    assert row is None
