from decimal import Decimal

from app.core.models import BotSettings, ExchangeName
from app.services.mt4_bridge import Mt4QuoteIn, Mt4QuoteStore, Mt4SpreadScanner, instrument_info


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
    assert item.mt4_contract_size == Decimal("1.000000")
    assert item.mt4_lots == Decimal("0.999001")
    assert item.hedge_base_quantity == Decimal("0.999001")
    assert item.estimated_exchange_funding_net == Decimal("0.0200")
    assert candidates


def test_mt4_spread_scanner_hides_unmatched_markets(tmp_path) -> None:
    store = Mt4QuoteStore(tmp_path / "quotes.json")
    store.update(Mt4QuoteIn(symbol="META", bid=Decimal("100"), ask=Decimal("100.1"), instrument_type="stock"))
    scanner = _MissingMarketScanner(store)

    opportunities, candidates, issues = scanner.scan(BotSettings())

    assert opportunities == []
    assert candidates == []
    assert issues == []


def test_mt4_gold_uses_lot_contract_size_for_overnight(tmp_path) -> None:
    store = Mt4QuoteStore(tmp_path / "quotes.json")
    quote = store.update(
        Mt4QuoteIn(
            symbol="XAUUSD",
            bid=Decimal("100"),
            ask=Decimal("100"),
            instrument_type="commodity",
            contract_size=Decimal("1"),
            overnight_long_usdt=Decimal("-100"),
            overnight_short_usdt=Decimal("50"),
        )
    )
    scanner = _GoldScanner(store)
    settings = BotSettings(mt4_notional_usdt=Decimal("1000"), mt4_min_spread_pct=Decimal("0.5"))

    opportunities, _candidates, issues = scanner.scan(settings)

    assert issues == []
    assert quote.contract_size == Decimal("100")
    item = opportunities[0]
    assert item.mt4_contract_size == Decimal("100.000000")
    assert item.hedge_base_quantity == Decimal("10.000000")
    assert item.mt4_lots == Decimal("0.100000")
    assert item.estimated_mt4_overnight_net == Decimal("-10.0000")


def test_mt4_gold_uses_minimum_lot_as_minimum_margin(tmp_path) -> None:
    store = Mt4QuoteStore(tmp_path / "quotes.json")
    store.update(
        Mt4QuoteIn(
            symbol="XAUUSD",
            bid=Decimal("2000"),
            ask=Decimal("2000.5"),
            instrument_type="commodity",
            overnight_long_usdt=Decimal("-100"),
            overnight_short_usdt=Decimal("50"),
        )
    )
    scanner = _GoldScanner(store)
    settings = BotSettings(mt4_notional_usdt=Decimal("100"), mt4_default_leverage=Decimal("5"))

    opportunities, _candidates, issues = scanner.scan(settings)

    assert issues == []
    item = opportunities[0]
    assert item.mt4_contract_size == Decimal("100.000000")
    assert item.mt4_lots == Decimal("0.010000")
    assert item.hedge_base_quantity == Decimal("1.000000")
    assert item.notional_usdt == Decimal("2000.00")
    assert item.margin_required_usdt == Decimal("400.00")
    assert item.estimated_mt4_overnight_net == Decimal("0.5000")


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


def test_mt4_quote_store_maps_crude_oil_symbols_with_broker_suffix(tmp_path) -> None:
    store = Mt4QuoteStore(tmp_path / "quotes.json")

    xti = store.update(Mt4QuoteIn(symbol="XTIUSD.pro", bid=Decimal("70"), ask=Decimal("70.1"), contract_size=Decimal("1000")))
    xbr = store.update(Mt4QuoteIn(symbol="XBRUSD#", bid=Decimal("80"), ask=Decimal("80.1"), contract_size=Decimal("1000")))

    assert xti.symbol == "XTIUSD"
    assert xbr.symbol == "XBRUSD"
    assert xti.contract_size == Decimal("1000")
    assert instrument_info("XTIUSD")["aliases"][0] == "CLUSDT"
    assert instrument_info("XBRUSD")["aliases"][0] == "BZUSDT"


def test_mt4_scanner_batches_exchange_tickers_for_matched_symbols(tmp_path) -> None:
    store = Mt4QuoteStore(tmp_path / "quotes.json")
    store.update(Mt4QuoteIn(symbol="XTIUSD", bid=Decimal("70"), ask=Decimal("70.1"), contract_size=Decimal("1000")))
    store.update(Mt4QuoteIn(symbol="XBRUSD", bid=Decimal("80"), ask=Decimal("80.1"), contract_size=Decimal("1000")))
    scanner = _BatchScanner(store)
    settings = BotSettings(mt4_notional_usdt=Decimal("100"), mt4_min_spread_pct=Decimal("0.5"))

    opportunities, _candidates, issues = scanner.scan(settings)

    assert issues == []
    assert {item.instrument for item in opportunities} == {"XTIUSD", "XBRUSD"}
    assert {item.exchange_symbol for item in opportunities} == {"CLUSDT", "BZUSDT"}
    assert scanner.exchange.fetch_tickers_count == 1
    assert scanner.exchange.fetch_ticker_count == 0


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

    def _exchange_rows_for_quotes(self, exchange, quotes, settings, issues):
        if exchange != ExchangeName.BINANCE:
            return []
        return super()._exchange_rows_for_quotes(exchange, quotes, settings, issues)


class _Exchange:
    def fetch_ticker(self, symbol):
        return {"bid": "101.5", "ask": "101.6"}


class _GoogleScanner(_Scanner):
    def _markets(self, exchange_name, exchange):
        return {"GOOGLUSDT": "GOOGL/USDT:USDT"}

    def _funding(self, exchange_name, exchange):
        return {"GOOGLUSDT": Decimal("0.0002")}


class _MissingMarketScanner(_Scanner):
    def _markets(self, exchange_name, exchange):
        return {}


class _GoldScanner(_Scanner):
    def _markets(self, exchange_name, exchange):
        return {"XAUUSDT": "XAU/USDT:USDT"}

    def _funding(self, exchange_name, exchange):
        return {"XAUUSDT": Decimal("0")}


class _BatchScanner(_Scanner):
    def __init__(self, quote_store):
        super().__init__(quote_store)
        self.exchange = _BatchExchange()

    def _exchange(self, exchange_name):
        return self.exchange

    def _markets(self, exchange_name, exchange):
        return {"CLUSDT": "CL/USDT:USDT", "BZUSDT": "BZ/USDT:USDT"}

    def _funding(self, exchange_name, exchange):
        return {"CLUSDT": Decimal("0"), "BZUSDT": Decimal("0")}


class _BatchExchange:
    has = {"fetchTickers": True}

    def __init__(self) -> None:
        self.fetch_tickers_count = 0
        self.fetch_ticker_count = 0

    def fetch_tickers(self, symbols):
        self.fetch_tickers_count += 1
        assert set(symbols) == {"CL/USDT:USDT", "BZ/USDT:USDT"}
        return {
            "CL/USDT:USDT": {"symbol": "CL/USDT:USDT", "bid": "72", "ask": "72.1"},
            "BZ/USDT:USDT": {"symbol": "BZ/USDT:USDT", "bid": "82", "ask": "82.1"},
        }

    def fetch_ticker(self, symbol):
        self.fetch_ticker_count += 1
        return {"bid": "0", "ask": "0"}


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
