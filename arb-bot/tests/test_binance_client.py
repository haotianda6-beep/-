from decimal import Decimal

from app.binance_client import _parse_account_snapshot


def test_parse_binance_futures_account_snapshot():
    account = _parse_account_snapshot(
        {
            "totalWalletBalance": "1000.5",
            "totalMarginBalance": "1008.25",
            "availableBalance": "900.1",
            "totalInitialMargin": "108.15",
            "totalUnrealizedProfit": "7.75",
        }
    )

    assert account.venue == "币安合约"
    assert account.balance == Decimal("1000.5")
    assert account.equity == Decimal("1008.25")
    assert account.available == Decimal("900.1")
    assert account.used_margin == Decimal("108.15")
    assert account.unrealized_pnl == Decimal("7.75")
    assert account.currency == "USDT"
