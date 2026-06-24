from decimal import Decimal

from app.models import MarketQuote
from app.quote_guard import xau_quote_gap_reason


def test_xau_quote_gap_reason_blocks_999_like_bad_tick():
    reason = xau_quote_gap_reason(
        MarketQuote(symbol="XAUUSDT", bid=Decimal("4000"), ask=Decimal("4000.2")),
        MarketQuote(symbol="XAUUSD", bid=Decimal("3000"), ask=Decimal("3000.2")),
    )

    assert reason is not None
    assert "超过 100 美元" in reason


def test_xau_quote_gap_reason_allows_normal_gold_gap():
    reason = xau_quote_gap_reason(
        MarketQuote(symbol="XAUUSDT", bid=Decimal("4000"), ask=Decimal("4000.2")),
        MarketQuote(symbol="XAUUSD", bid=Decimal("3997"), ask=Decimal("3997.3")),
    )

    assert reason is None
