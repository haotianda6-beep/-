from decimal import Decimal

from app.core.models import BotSettings, ExchangeName
from app.services.mt4_bridge import Mt4QuoteIn, Mt4QuoteStore, Mt4SpreadScanner


def test_mt4_quote_store_persists_quote_and_estimates_overnight(tmp_path) -> None:
    store = Mt4QuoteStore(tmp_path / "quotes.json")

    quote = store.update(
        Mt4QuoteIn(
            symbol="XAUUSD",
            bid=Decimal("2300"),
            ask=Decimal("2300.5"),
            instrument_type="commodity",
            tick_value=Decimal("1"),
            tick_size=Decimal("0.01"),
            swap_long_points=Decimal("-0.12"),
            swap_short_points=Decimal("0.04"),
        )
    )

    assert quote.symbol == "XAUUSD"
    assert quote.overnight_long_usdt == Decimal("-12")
    assert quote.overnight_short_usdt == Decimal("4")
    assert store.quotes(Decimal("10"))[0].symbol == "XAUUSD"


def test_mt4_spread_scanner_builds_candidate_with_funding_and_overnight(tmp_path) -> None:
    store = Mt4QuoteStore(tmp_path / "quotes.json")
    store.update(
        Mt4QuoteIn(
            symbol="AAPL",
            bid=Decimal("100"),
            ask=Decimal("100.1"),
            instrument_type="stock",
            overnight_long_usdt=Decimal("-0.01"),
            overnight_short_usdt=Decimal("0.02"),
        )
    )
    scanner = _Scanner(store)
    settings = BotSettings(mt4_notional_usdt=Decimal("100"), mt4_min_spread_pct=Decimal("0.5"), mt4_default_leverage=Decimal("5"))

    opportunities, candidates, issues = scanner.scan(settings)

    assert issues == []
    assert opportunities
    item = opportunities[0]
    assert item.instrument == "AAPL"
    assert item.exchange == ExchangeName.BINANCE
    assert item.exchange_symbol == "AAPLUSDT"
    assert item.long_venue == "MT4"
    assert item.short_venue == "BINANCE"
    assert item.margin_required_usdt == Decimal("20.00")
    assert item.estimated_exchange_funding_net == Decimal("0.0200")
    assert candidates


def test_mt4_quote_store_maps_stock_symbols_with_broker_suffix(tmp_path) -> None:
    store = Mt4QuoteStore(tmp_path / "quotes.json")

    quote = store.update(
        Mt4QuoteIn(
            symbol="AAPL.cash",
            bid=Decimal("100"),
            ask=Decimal("100.1"),
            instrument_type="stock",
        )
    )

    assert quote.symbol == "AAPL"
    assert store.quotes(Decimal("10"))[0].symbol == "AAPL"


def test_mt4_quote_store_maps_google_nasdaq_symbol_to_exchange_alias(tmp_path) -> None:
    store = Mt4QuoteStore(tmp_path / "quotes.json")
    store.update(Mt4QuoteIn(symbol="GOOG.NAS", bid=Decimal("100"), ask=Decimal("100.1"), instrument_type="stock"))
    scanner = _GoogleScanner(store)
    settings = BotSettings(mt4_notional_usdt=Decimal("100"), mt4_min_spread_pct=Decimal("0.5"))

    opportunities, _candidates, _issues = scanner.scan(settings)

    assert opportunities[0].instrument == "GOOG"
    assert opportunities[0].exchange_symbol == "GOOGLUSDT"


def test_mt4_scanner_cleans_okx_markets_with_empty_symbols() -> None:
    scanner = Mt4SpreadScanner()
    first = _OkxMarketExchange()
    markets = scanner._markets(ExchangeName.OKX, first)
    second = _OkxMarketExchange()
    cached_markets = scanner._markets(ExchangeName.OKX, second)

    assert markets == {"AAPLUSDT": "AAPL/USDT:USDT"}
    assert cached_markets == markets
    assert first.fetch_count == 1
    assert second.fetch_count == 0
    assert second.markets == {"AAPL/USDT:USDT": {"id": "AAPL-USDT-SWAP", "symbol": "AAPL/USDT:USDT", "base": "AAPL", "quote": "USDT", "swap": True, "active": True}}


class _Scanner(Mt4SpreadScanner):
    def _exchange(self, exchange_name):
        return _Exchange()

    def _markets(self, exchange_name, exchange):
        return {"AAPLUSDT": "AAPL/USDT:USDT"}

    def _funding(self, exchange_name, exchange):
        return {"AAPLUSDT": Decimal("0.0002")}

    def _exchange_rows(self, exchange, quote, settings, issues):
        if exchange != ExchangeName.BINANCE:
            return []
        return super()._exchange_rows(exchange, quote, settings, issues)


class _Exchange:
    def fetch_ticker(self, symbol):
        return {"bid": "101.5", "ask": "101.6"}


class _GoogleScanner(_Scanner):
    def _markets(self, exchange_name, exchange):
        return {"GOOGLUSDT": "GOOGL/USDT:USDT"}

    def _funding(self, exchange_name, exchange):
        return {"GOOGLUSDT": Decimal("0.0002")}


class _OkxMarketExchange:
    def __init__(self) -> None:
        self.markets = {}
        self.fetch_count = 0

    def fetch_markets(self, params):
        self.fetch_count += 1
        assert params == {"instType": "SWAP"}
        return [
            {"id": None, "symbol": None, "swap": False, "quote": "", "active": False},
            {"id": "AAPL-USDT-SWAP", "symbol": "AAPL/USDT:USDT", "base": "AAPL", "quote": "USDT", "swap": True, "active": True},
        ]

    def set_markets(self, markets):
        self.markets = {market["symbol"]: market for market in markets}
