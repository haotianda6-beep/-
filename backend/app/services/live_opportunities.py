from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.core.market_math import FEE_RATES, q
from app.core.models import BotSettings, DataSource, ExchangeName, Opportunity, OpportunityCandidate
from app.core.pnl import calculate_funding_net, calculate_spread_pct
from app.services.asset_identity import MarketAsset, asset_from_market, local_identity_reasons
from app.services.exchange_factory import build_ccxt_exchange
from app.services.market_checks import TransferNetworks, bidirectional_route_check, extract_transfer_networks, has_depth
from app.services.live_market_types import ExchangeMarketData, LiveOpportunityScan, SPOT_EXCHANGE_IDS, SWAP_EXCHANGE_IDS, SwapMarket
from app.services.market_format import normalize_ccxt_symbol, normalized_market_symbol, quote_volume
from app.services.live_read import decimal_from
from app.services.opportunity_reasons import base_filter_reasons


class LiveOpportunityScanner:
    def __init__(self) -> None:
        self._market_cache: dict[ExchangeName, tuple[datetime, dict[str, SwapMarket], dict[str, MarketAsset]]] = {}
        self._currency_cache: dict[ExchangeName, tuple[datetime, dict[str, TransferNetworks], bool]] = {}
        self._funding_cache: dict[ExchangeName, tuple[datetime, dict[str, Decimal]]] = {}

    def clear_caches(self) -> None:
        self._market_cache = {}
        self._currency_cache = {}
        self._funding_cache = {}

    def scan(self, settings: BotSettings) -> LiveOpportunityScan:
        exchanges = list(ExchangeName)
        with ThreadPoolExecutor(max_workers=min(3, len(exchanges))) as executor:
            data = list(executor.map(self._load_exchange_data, exchanges))
        issues = [issue for item in data for issue in item.issues]
        by_exchange = {item.exchange: item for item in data}
        candidates: list[tuple[Opportunity, list[str]]] = []
        for long_exchange in ExchangeName:
            for short_exchange in ExchangeName:
                if long_exchange != short_exchange and not self._is_blocked(long_exchange, short_exchange, settings):
                    candidates.extend(self._pair_opportunities(by_exchange[long_exchange], by_exchange[short_exchange], settings))
        sorted_candidates = sorted(candidates, key=lambda item: (len(item[1]), -item[0].estimated_net_profit))[:25]
        checked = [self._validate_opportunity(candidate, reasons, by_exchange, settings) for candidate, reasons in sorted_candidates]
        opportunities = [item for item, reasons in checked if not reasons]
        ranked = sorted(checked, key=lambda item: (len(item[1]), -item[0].estimated_net_profit))
        display_candidates = [self._candidate_from(item, reasons) for item, reasons in ranked]
        return LiveOpportunityScan(opportunities=sorted(opportunities, key=lambda item: item.estimated_net_profit, reverse=True), candidates=display_candidates, issues=issues)

    def _load_exchange_data(self, exchange_name: ExchangeName) -> ExchangeMarketData:
        result = ExchangeMarketData(exchange=exchange_name)
        try:
            swap_exchange = self._build_exchange(exchange_name, SWAP_EXCHANGE_IDS[exchange_name], "swap")
            spot_exchange = self._build_exchange(exchange_name, SPOT_EXCHANGE_IDS[exchange_name], "spot")
            result.swap_exchange = swap_exchange
            result.swaps, result.spot_markets = self._get_cached_markets(exchange_name, swap_exchange, spot_exchange)
            result.transfer_networks, result.transfer_query_ok = self._get_cached_currencies(exchange_name, spot_exchange, result.issues)
            result.tickers = self._fetch_tickers(swap_exchange, result.swaps, exchange_name, result.issues)
            result.funding_rates = self._fetch_funding_rates(swap_exchange, result.swaps, exchange_name, result.issues)
        except Exception as exc:  # noqa: BLE001 - ccxt uses exchange-specific exceptions.
            result.issues.append(f"{exchange_name}: 市场数据读取失败 {str(exc)[:220]}")
        return result

    def _build_exchange(self, exchange_name: ExchangeName, exchange_id: str, default_type: str):
        return build_ccxt_exchange(exchange_name, exchange_id, default_type, timeout=15000)

    def _get_cached_markets(self, exchange_name: ExchangeName, swap_exchange, spot_exchange) -> tuple[dict[str, SwapMarket], dict[str, MarketAsset]]:
        now = datetime.now(timezone.utc)
        cached = self._market_cache.get(exchange_name)
        if cached and (now - cached[0]).total_seconds() < 600:
            return cached[1], cached[2]
        swaps = self._extract_swaps(swap_exchange.load_markets())
        spots = self._extract_spots(spot_exchange.load_markets())
        self._market_cache[exchange_name] = (now, swaps, spots)
        return swaps, spots

    def _get_cached_currencies(
        self,
        exchange_name: ExchangeName,
        spot_exchange,
        issues: list[str],
    ) -> tuple[dict[str, TransferNetworks], bool]:
        now = datetime.now(timezone.utc)
        cached = self._currency_cache.get(exchange_name)
        if cached and (now - cached[0]).total_seconds() < 600:
            return cached[1], cached[2]
        if not spot_exchange.has.get("fetchCurrencies"):
            issues.append(f"{exchange_name}: 不支持币种链路查询")
            return {}, False
        try:
            networks = extract_transfer_networks(spot_exchange.fetch_currencies())
            self._currency_cache[exchange_name] = (now, networks, True)
            return networks, True
        except Exception as exc:  # noqa: BLE001
            issues.append(f"{exchange_name}: 币种链路读取失败 {str(exc)[:180]}")
            return {}, False

    def _extract_swaps(self, markets: dict[str, Any]) -> dict[str, SwapMarket]:
        swaps: dict[str, SwapMarket] = {}
        for market in markets.values():
            if not market.get("swap") or market.get("quote") != "USDT":
                continue
            if market.get("settle") not in (None, "USDT"):
                continue
            if market.get("active") is False:
                continue
            normalized = normalized_market_symbol(market)
            taker = decimal_from(market.get("taker"), "0")
            swaps[normalized] = SwapMarket(
                symbol=normalized,
                ccxt_symbol=market["symbol"],
                taker_fee=taker if taker > 0 else Decimal("0"),
                asset=asset_from_market(market),
            )
        return swaps

    def _extract_spots(self, markets: dict[str, Any]) -> dict[str, MarketAsset]:
        spots: dict[str, MarketAsset] = {}
        for market in markets.values():
            if not market.get("spot") or market.get("quote") != "USDT":
                continue
            if market.get("active") is False:
                continue
            spots[normalized_market_symbol(market)] = asset_from_market(market)
        return spots

    def _fetch_tickers(
        self,
        exchange,
        swaps: dict[str, SwapMarket],
        exchange_name: ExchangeName,
        issues: list[str],
    ) -> dict[str, dict[str, Any]]:
        if not exchange.has.get("fetchTickers"):
            issues.append(f"{exchange_name}: 不支持批量 tickers")
            return {}
        try:
            raw = exchange.fetch_tickers([market.ccxt_symbol for market in swaps.values()])
        except Exception:
            try:
                raw = exchange.fetch_tickers()
            except Exception as exc:  # noqa: BLE001
                issues.append(f"{exchange_name}: tickers 读取失败 {str(exc)[:220]}")
                return {}
        return {normalize_ccxt_symbol(symbol): ticker for symbol, ticker in raw.items()}

    def _fetch_funding_rates(
        self,
        exchange,
        swaps: dict[str, SwapMarket],
        exchange_name: ExchangeName,
        issues: list[str],
    ) -> dict[str, Decimal]:
        if not exchange.has.get("fetchFundingRates"):
            issues.append(f"{exchange_name}: 不支持批量 funding rates")
            return {}
        now = datetime.now(timezone.utc)
        cached = self._funding_cache.get(exchange_name)
        if cached and (now - cached[0]).total_seconds() < 60:
            return cached[1]
        try:
            raw = exchange.fetch_funding_rates([market.ccxt_symbol for market in swaps.values()])
        except Exception:
            try:
                raw = exchange.fetch_funding_rates()
            except Exception as exc:  # noqa: BLE001
                issues.append(f"{exchange_name}: funding rates 读取失败 {str(exc)[:220]}")
                return {}
        items = raw.values() if isinstance(raw, dict) else raw
        rates: dict[str, Decimal] = {}
        for item in items:
            symbol = item.get("symbol")
            if not symbol:
                continue
            rate = decimal_from(item.get("fundingRate") or item.get("nextFundingRate"))
            rates[normalize_ccxt_symbol(symbol)] = rate
        self._funding_cache[exchange_name] = (now, rates)
        return rates

    def _pair_opportunities(
        self,
        long_data: ExchangeMarketData,
        short_data: ExchangeMarketData,
        settings: BotSettings,
    ) -> list[tuple[Opportunity, list[str]]]:
        opportunities: list[tuple[Opportunity, list[str]]] = []
        for symbol in sorted(set(long_data.swaps) & set(short_data.swaps)):
            if symbol in settings.symbol_blacklist:
                continue
            item = self._build_opportunity(symbol, long_data, short_data, settings)
            if item:
                opportunities.append(item)
        return opportunities

    def _build_opportunity(
        self,
        symbol: str,
        long_data: ExchangeMarketData,
        short_data: ExchangeMarketData,
        settings: BotSettings,
    ) -> tuple[Opportunity, list[str]] | None:
        long_ticker = long_data.tickers.get(symbol)
        short_ticker = short_data.tickers.get(symbol)
        if not long_ticker or not short_ticker:
            return None
        long_price = decimal_from(long_ticker.get("ask"))
        short_price = decimal_from(short_ticker.get("bid"))
        if long_price <= 0 or short_price <= 0:
            return None
        spread_pct = calculate_spread_pct(long_price, short_price)
        long_volume = quote_volume(long_ticker)
        short_volume = quote_volume(short_ticker)
        min_volume = min(long_volume, short_volume)
        long_funding = long_data.funding_rates.get(symbol, Decimal("0"))
        short_funding = short_data.funding_rates.get(symbol, Decimal("0"))
        funding_net = calculate_funding_net(settings.order_notional_usdt, long_funding, short_funding)
        long_fee = long_data.swaps[symbol].taker_fee or FEE_RATES[long_data.exchange]
        short_fee = short_data.swaps[symbol].taker_fee or FEE_RATES[short_data.exchange]
        fees = settings.order_notional_usdt * (long_fee + short_fee) * Decimal("2")
        gross = settings.order_notional_usdt * spread_pct / Decimal("100")
        estimated_net = gross - fees + funding_net
        spot_ok = symbol in long_data.spot_markets and symbol in short_data.spot_markets
        reasons = base_filter_reasons(spread_pct, funding_net, min_volume, spot_ok, settings)
        reasons.extend(local_identity_reasons(long_data.exchange.value, long_data.swaps[symbol].asset, long_data.spot_markets.get(symbol)))
        reasons.extend(local_identity_reasons(short_data.exchange.value, short_data.swaps[symbol].asset, short_data.spot_markets.get(symbol)))
        return Opportunity(
            symbol=symbol,
            long_exchange=long_data.exchange,
            short_exchange=short_data.exchange,
            long_price=q(long_price),
            short_price=q(short_price),
            spread_pct=q(spread_pct),
            long_volume_24h_usdt=q(long_volume, "0.01"),
            short_volume_24h_usdt=q(short_volume, "0.01"),
            min_volume_24h_usdt=q(min_volume, "0.01"),
            estimated_open_close_fee=q(fees),
            estimated_funding_net=q(funding_net),
            estimated_net_profit=q(estimated_net),
            notional_usdt=q(settings.order_notional_usdt, "0.01"),
            margin_required_usdt=q(settings.order_notional_usdt / settings.default_leverage if settings.default_leverage > 0 else settings.order_notional_usdt, "0.01"),
            leverage=settings.default_leverage,
            spot_transfer_ok=False,
            depth_ok=False,
            risk_tags=["execution-check-pending"],
            data_source=DataSource.LIVE,
            updated_at=datetime.now(timezone.utc),
        ), reasons

    def _validate_opportunity(
        self,
        opportunity: Opportunity,
        base_reasons: list[str],
        data: dict[ExchangeName, ExchangeMarketData],
        settings: BotSettings,
    ) -> tuple[Opportunity, list[str]]:
        long_data = data[ExchangeName(opportunity.long_exchange)]
        short_data = data[ExchangeName(opportunity.short_exchange)]
        reasons = list(base_reasons)
        route = bidirectional_route_check(
            opportunity.symbol.removesuffix("USDT"),
            long_data.transfer_networks,
            short_data.transfer_networks,
            long_data.exchange.value,
            short_data.exchange.value,
            long_data.transfer_query_ok,
            short_data.transfer_query_ok,
        )
        depth_ok = self._depth_ok(opportunity.symbol, opportunity.long_price, opportunity.short_price, long_data, short_data, settings)
        if not route.ok:
            reasons.extend(route.reasons)
        if not depth_ok:
            reasons.append("盘口深度不足或无法在滑点内成交")
        return opportunity.model_copy(update={
            "spot_transfer_ok": route.ok,
            "depth_ok": depth_ok,
            "risk_tags": [] if not reasons else ["blocked"],
            "updated_at": datetime.now(timezone.utc),
        }), reasons

    def _candidate_from(self, opportunity: Opportunity, reasons: list[str]) -> OpportunityCandidate:
        label = reasons or ["满足当前开仓条件，已进入可开仓机会表"]
        return OpportunityCandidate(**opportunity.model_dump(), blocked_reasons=label)

    def _depth_ok(
        self,
        symbol: str,
        long_price: Decimal,
        short_price: Decimal,
        long_data: ExchangeMarketData,
        short_data: ExchangeMarketData,
        settings: BotSettings,
    ) -> bool:
        long_market = long_data.swaps.get(symbol)
        short_market = short_data.swaps.get(symbol)
        if not long_market or not short_market or not long_data.swap_exchange or not short_data.swap_exchange:
            return False
        required = settings.order_notional_usdt * Decimal("3")
        try:
            return has_depth(long_data.swap_exchange, long_market.ccxt_symbol, "long", long_price, required, settings.max_slippage_pct) and has_depth(short_data.swap_exchange, short_market.ccxt_symbol, "short", short_price, required, settings.max_slippage_pct)
        except Exception:
            return False

    def _is_blocked(self, long_exchange: ExchangeName, short_exchange: ExchangeName, settings: BotSettings) -> bool:
        return long_exchange in set(settings.exchange_blacklist) or short_exchange in set(settings.exchange_blacklist)
