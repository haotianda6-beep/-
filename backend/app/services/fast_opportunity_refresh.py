from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.core.market_math import q
from app.core.models import BotSettings, ExchangeName, Opportunity, OpportunityCandidate
from app.core.pnl import calculate_spread_pct
from app.services.asset_identity import asset_from_market
from app.services.exchange_factory import build_ccxt_exchange
from app.services.live_market_types import LiveOpportunityScan, SWAP_EXCHANGE_IDS, SwapMarket
from app.services.live_read import decimal_from
from app.services.market_format import normalize_ccxt_symbol, normalized_market_symbol, quote_volume
from app.services.ws_ticker_cache import WSTickerCache


class FastOpportunityRefresher:
    def __init__(self, ticker_cache: WSTickerCache | None = None) -> None:
        self._market_cache: dict[str, tuple[datetime, dict[str, SwapMarket]]] = {}
        self.ticker_cache = ticker_cache

    def refresh(self, scan: LiveOpportunityScan, settings: BotSettings) -> LiveOpportunityScan:
        tracked = list(scan.opportunities) + list(scan.candidates)
        if not tracked:
            return scan
        needed = self._needed_symbols(tracked)
        with ThreadPoolExecutor(max_workers=max(1, len(needed))) as executor:
            ticker_items = executor.map(lambda item: self._fetch_tickers(*item), needed.items())
        tickers = dict(ticker_items)
        refreshed = [
            item
            for item in (self._refresh_one(opportunity, tickers, settings) for opportunity in scan.opportunities)
            if item is not None
        ]
        refreshed_candidates = [
            item
            for item in (self._refresh_candidate(candidate, tickers, settings) for candidate in scan.candidates)
            if item is not None
        ]
        return LiveOpportunityScan(
            opportunities=sorted(refreshed, key=lambda item: item.estimated_net_profit, reverse=True),
            candidates=sorted(refreshed_candidates, key=lambda item: (len(item.blocked_reasons), -item.estimated_net_profit)),
            issues=scan.issues,
        )

    def _needed_symbols(self, opportunities: list[Opportunity]) -> dict[str, set[str]]:
        needed: dict[str, set[str]] = {}
        for item in opportunities:
            needed.setdefault(str(item.long_exchange), set()).add(item.symbol)
            needed.setdefault(str(item.short_exchange), set()).add(item.symbol)
        return needed

    def _fetch_tickers(self, exchange_name: str, symbols: set[str]) -> tuple[str, dict[str, dict[str, Any]]]:
        exchange = self._build_exchange(exchange_name)
        markets = self._markets(exchange_name, exchange)
        if self.ticker_cache:
            return exchange_name, self._cached_tickers(exchange_name, symbols, markets)
        ccxt_symbols = [markets[symbol].ccxt_symbol for symbol in symbols if symbol in markets]
        if not ccxt_symbols:
            return exchange_name, {}
        try:
            raw = exchange.fetch_tickers(ccxt_symbols) if exchange.has.get("fetchTickers") else {}
        except Exception:
            raw = {}
        if not raw:
            raw = self._fetch_tickers_one_by_one(exchange, ccxt_symbols)
        return exchange_name, {normalize_ccxt_symbol(symbol): ticker for symbol, ticker in raw.items()}

    def _build_exchange(self, exchange_name: str):
        exchange = ExchangeName(exchange_name)
        return build_ccxt_exchange(exchange, SWAP_EXCHANGE_IDS[exchange], "swap", timeout=5000)

    def _markets(self, exchange_name: str, exchange) -> dict[str, SwapMarket]:
        now = datetime.now(timezone.utc)
        cached = self._market_cache.get(exchange_name)
        if cached and (now - cached[0]).total_seconds() < 600:
            return cached[1]
        markets = {}
        for market in exchange.load_markets().values():
            if not market.get("swap") or market.get("quote") != "USDT" or market.get("active") is False:
                continue
            symbol = normalized_market_symbol(market)
            markets[symbol] = SwapMarket(symbol=symbol, ccxt_symbol=market["symbol"], taker_fee=Decimal("0"), asset=asset_from_market(market))
        self._market_cache[exchange_name] = (now, markets)
        return markets

    def _fetch_tickers_one_by_one(self, exchange, symbols: list[str]) -> dict[str, dict[str, Any]]:
        result = {}
        for symbol in symbols:
            try:
                result[symbol] = exchange.fetch_ticker(symbol)
            except Exception:
                continue
        return result

    def _cached_tickers(self, exchange_name: str, symbols: set[str], markets: dict[str, SwapMarket]) -> dict[str, dict[str, Any]]:
        result = {}
        exchange = ExchangeName(exchange_name)
        for symbol in symbols:
            market = markets.get(symbol)
            if not market:
                continue
            self.ticker_cache.subscribe(exchange, "swap", symbol, market.ccxt_symbol)
            ticker = self.ticker_cache.get(exchange, "swap", symbol)
            if ticker:
                result[symbol] = ticker
        return result

    def _refresh_one(
        self,
        opportunity: Opportunity,
        tickers: dict[str, dict[str, dict[str, Any]]],
        settings: BotSettings,
        enforce_filters: bool = True,
    ) -> Opportunity | None:
        long_ticker = tickers.get(str(opportunity.long_exchange), {}).get(opportunity.symbol)
        short_ticker = tickers.get(str(opportunity.short_exchange), {}).get(opportunity.symbol)
        if not long_ticker or not short_ticker:
            return opportunity
        long_price = decimal_from(long_ticker.get("ask") or long_ticker.get("last") or long_ticker.get("close"))
        short_price = decimal_from(short_ticker.get("bid") or short_ticker.get("last") or short_ticker.get("close"))
        if long_price <= 0 or short_price <= 0:
            return opportunity
        spread_pct = calculate_spread_pct(long_price, short_price)
        long_volume = quote_volume(long_ticker) or opportunity.long_volume_24h_usdt
        short_volume = quote_volume(short_ticker) or opportunity.short_volume_24h_usdt
        min_volume = min(long_volume, short_volume)
        gross = settings.order_notional_usdt * spread_pct / Decimal("100")
        net = gross - opportunity.estimated_open_close_fee + opportunity.estimated_funding_net
        if enforce_filters and (
            spread_pct < settings.min_open_spread_pct
            or min_volume < settings.min_24h_volume_usdt
            or opportunity.estimated_funding_net < settings.min_funding_net_usdt
        ):
            return None
        return opportunity.model_copy(update={
            "long_price": q(long_price),
            "short_price": q(short_price),
            "spread_pct": q(spread_pct),
            "long_volume_24h_usdt": q(long_volume, "0.01"),
            "short_volume_24h_usdt": q(short_volume, "0.01"),
            "min_volume_24h_usdt": q(min_volume, "0.01"),
            "estimated_net_profit": q(net),
            "updated_at": datetime.now(timezone.utc),
        })

    def _refresh_candidate(
        self,
        candidate: OpportunityCandidate,
        tickers: dict[str, dict[str, dict[str, Any]]],
        settings: BotSettings,
    ) -> OpportunityCandidate | None:
        refreshed = self._refresh_one(candidate, tickers, settings, enforce_filters=False)
        if refreshed is None:
            return None
        return OpportunityCandidate(**refreshed.model_dump(exclude={"blocked_reasons"}), blocked_reasons=candidate.blocked_reasons)
