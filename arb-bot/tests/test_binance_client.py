from decimal import Decimal

import pytest

from app.binance_client import BinanceError, BinanceFuturesClient, _parse_account_snapshot
from app.config import Settings
from app.models import OrderRequest, Side


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


@pytest.mark.asyncio
async def test_live_binance_market_order_is_disabled(tmp_path):
    cfg = Settings(
        _env_file=None,
        SQLITE_PATH=tmp_path / "test.sqlite3",
        LIVE_TRADING=True,
        PAPER_MODE=False,
        BINANCE_API_KEY="key",
        BINANCE_API_SECRET="secret",
    )
    client = BinanceFuturesClient(cfg)

    with pytest.raises(BinanceError, match="市价单已被禁用"):
        await client.place_market_order(
            OrderRequest(symbol=cfg.binance_symbol, side=Side.BUY, quantity=Decimal("1"), reduce_only=True)
        )

    await client.stop()
