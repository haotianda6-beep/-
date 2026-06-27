from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.core.market_math import FEE_RATES, q
from app.core.models import BotSettings, CashCarryOpportunity, DataSource, ExchangeName
from app.core.pnl import calculate_spread_pct
from app.services.asset_identity import MarketAsset, asset_from_market, local_identity_reasons
from app.services.account_fee_rates import account_taker_fee_map
from app.services.cash_carry_depth_estimator import estimate_max_safe_notional
from app.services.cash_carry_history_quality import CashCarryHistoryQuality
from app.services.cash_carry_quality import cash_carry_candidate_sort_key, cash_carry_quality_score, entry_basis_risk_reasons, estimated_entry_net_profit, convergence_basis_profit
from app.services.cash_carry_scope import CASH_CARRY_EXCHANGES, CASH_CARRY_INTERNAL_CANDIDATE_LIMIT
from app.services.exchange_factory import build_ccxt_exchange
from app.services.live_market_types import CashCarryScan, SPOT_EXCHANGE_IDS, SWAP_EXCHANGE_IDS
from app.services.live_read import decimal_from
from app.services.market_format import normalize_ccxt_symbol, normalized_market_symbol, quote_volume


@dataclass(frozen=True)
class TradeMarket:
    symbol: str
    ccxt_symbol: str
    taker_fee: Decimal
    asset: MarketAsset
    is_pre_market: bool = False
    deposit_enabled: bool | None = None
    withdraw_enabled: bool | None = None


@dataclass
class CashCarryExchangeData:
    exchange: ExchangeName
    spot_exchange: Any | None = None
    swap_exchange: Any | None = None
    spot_markets: dict[str, TradeMarket] = field(default_factory=dict)
    swap_markets: dict[str, TradeMarket] = field(default_factory=dict)
    spot_tickers: dict[str, dict[str, Any]] = field(default_factory=dict)
    swap_tickers: dict[str, dict[str, Any]] = field(default_factory=dict)
    funding_rates: dict[str, Decimal] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)


class CashCarryScanner:
    def __init__(self, history_quality: CashCarryHistoryQuality | None = None) -> None:
        self._market_cache: dict[ExchangeName, tuple[datetime, dict[str, TradeMarket], dict[str, TradeMarket]]] = {}
        self._funding_cache: dict[ExchangeName, tuple[datetime, dict[str, Decimal]]] = {}
        self.history_quality = history_quality or CashCarryHistoryQuality()

    def clear_caches(self) -> None:
        self._market_cache = {}
        self._funding_cache = {}

    def scan(self, settings: BotSettings) -> CashCarryScan:
        if not settings.cash_carry_enabled:
            return CashCarryScan()
        exchanges = [exchange for exchange in CASH_CARRY_EXCHANGES if exchange not in set(settings.exchange_blacklist)]
        with ThreadPoolExecutor(max_workers=max(1, min(3, len(exchanges)))) as executor:
            data = list(executor.map(self._load_exchange_data, exchanges))
        try:
            checked = [
                item
                for exchange_data in data
                for item in self._exchange_opportunities(exchange_data, settings)
            ]
            opportunities = [item for item in checked if not item.blocked_reasons]
            candidates = sorted(checked, key=lambda item: self._candidate_sort_key(item, settings))[:CASH_CARRY_INTERNAL_CANDIDATE_LIMIT]
            return CashCarryScan(
                opportunities=sorted(opportunities, key=lambda item: self._opportunity_sort_key(item, settings)),
                candidates=candidates,
                issues=[issue for item in data for issue in item.issues],
            )
        finally:
            for item in data:
                self._close_exchange(item.spot_exchange)
                self._close_exchange(item.swap_exchange)

    def _load_exchange_data(self, exchange_name: ExchangeName) -> CashCarryExchangeData:
        result = CashCarryExchangeData(exchange=exchange_name)
        try:
            spot_exchange = self._build_exchange(exchange_name, SPOT_EXCHANGE_IDS[exchange_name], "spot")
            swap_exchange = self._build_exchange(exchange_name, SWAP_EXCHANGE_IDS[exchange_name], "swap")
            result.spot_exchange = spot_exchange
            result.swap_exchange = swap_exchange
            result.spot_markets, result.swap_markets = self._get_cached_markets(exchange_name, spot_exchange, swap_exchange, result.issues)
            result.spot_tickers = self._fetch_tickers(spot_exchange, result.spot_markets, exchange_name, "现货", result.issues)
            result.swap_tickers = self._fetch_tickers(swap_exchange, result.swap_markets, exchange_name, "合约", result.issues)
            result.funding_rates = self._fetch_funding_rates(swap_exchange, result.swap_markets, exchange_name, result.issues)
        except Exception as exc:  # noqa: BLE001
            result.issues.append(f"{exchange_name}: 期现行情读取失败 {str(exc)[:220]}")
        return result

    def _build_exchange(self, exchange_name: ExchangeName, exchange_id: str, default_type: str):
        return build_ccxt_exchange(exchange_name, exchange_id, default_type, timeout=12000)

    def _close_exchange(self, exchange) -> None:
        close = getattr(exchange, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _get_cached_markets(self, exchange_name: ExchangeName, spot_exchange, swap_exchange, issues: list[str]):
        now = datetime.now(timezone.utc)
        cached = self._market_cache.get(exchange_name)
        if cached and (now - cached[0]).total_seconds() < 600:
            return cached[1], cached[2]
        spots = self._extract_markets(spot_exchange.load_markets(), "spot")
        swaps = self._extract_markets(swap_exchange.load_markets(), "swap")
        self._apply_spot_transfer_statuses(spots, spot_exchange, exchange_name, issues)
        self._apply_account_taker_fee_rates(spots, spot_exchange, exchange_name, "spot", issues)
        self._apply_account_taker_fee_rates(swaps, swap_exchange, exchange_name, "swap", issues)
        self._market_cache[exchange_name] = (now, spots, swaps)
        return spots, swaps

    def market_pair(self, exchange_name: ExchangeName, symbol: str) -> tuple[TradeMarket | None, TradeMarket | None]:
        cached = self._market_cache.get(exchange_name)
        if not cached:
            return None, None
        return cached[1].get(symbol), cached[2].get(symbol)

    def _extract_markets(self, markets: dict[str, Any], market_type: str) -> dict[str, TradeMarket]:
        result: dict[str, TradeMarket] = {}
        for market in markets.values():
            if not market.get(market_type) or market.get("quote") != "USDT" or market.get("active") is False:
                continue
            if market_type == "swap" and market.get("settle") not in (None, "USDT"):
                continue
            symbol = normalized_market_symbol(market)
            taker = decimal_from(market.get("taker"), "0")
            pre_market = self._is_pre_market(market) if market_type == "swap" else False
            result[symbol] = TradeMarket(symbol=symbol, ccxt_symbol=market["symbol"], taker_fee=taker, asset=asset_from_market(market), is_pre_market=pre_market)
        return result

    def _apply_account_taker_fee_rates(
        self,
        markets: dict[str, TradeMarket],
        exchange,
        exchange_name: ExchangeName,
        market_type: str,
        issues: list[str],
    ) -> None:
        fee_map = account_taker_fee_map(exchange_name, market_type, exchange, issues)
        if not fee_map:
            return
        for symbol, market in list(markets.items()):
            taker_fee = fee_map.get(market.ccxt_symbol)
            if taker_fee is not None and taker_fee > 0:
                markets[symbol] = replace(market, taker_fee=taker_fee)

    def _apply_spot_transfer_statuses(
        self,
        spots: dict[str, TradeMarket],
        spot_exchange,
        exchange_name: ExchangeName,
        issues: list[str],
    ) -> None:
        if not spot_exchange.has.get("fetchCurrencies"):
            issues.append(f"{exchange_name}: 不支持读取现货充提状态，预上市充提关闭过滤无法确认")
            return
        try:
            currencies = spot_exchange.fetch_currencies()
        except Exception as exc:  # noqa: BLE001
            issues.append(f"{exchange_name}: 现货充提状态读取失败 {str(exc)[:180]}")
            return
        for symbol, market in list(spots.items()):
            currency = currencies.get(market.asset.base)
            if not isinstance(currency, dict):
                continue
            spots[symbol] = replace(
                market,
                deposit_enabled=self._currency_capability(currency, "deposit"),
                withdraw_enabled=self._currency_capability(currency, "withdraw"),
            )

    def _currency_capability(self, currency: dict[str, Any], key: str) -> bool | None:
        direct = currency.get(key)
        if direct is not None:
            return bool(direct)
        networks = currency.get("networks")
        if not isinstance(networks, dict):
            return None
        values = []
        for network in networks.values():
            if not isinstance(network, dict):
                continue
            active = network.get("active")
            value = network.get(key)
            if value is not None:
                values.append(bool(value) and active is not False)
        return any(values) if values else None

    def _fetch_tickers(self, exchange, markets: dict[str, TradeMarket], exchange_name: ExchangeName, label: str, issues: list[str]):
        if not exchange.has.get("fetchTickers"):
            issues.append(f"{exchange_name}: 不支持批量读取{label} tickers")
            return {}
        symbols = [market.ccxt_symbol for market in markets.values()]
        try:
            raw = exchange.fetch_tickers(symbols)
        except Exception:
            try:
                raw = exchange.fetch_tickers()
            except Exception as exc:  # noqa: BLE001
                issues.append(f"{exchange_name}: {label} tickers 读取失败 {str(exc)[:180]}")
                return {}
        return {normalize_ccxt_symbol(symbol): ticker for symbol, ticker in raw.items()}

    def _fetch_funding_rates(
        self,
        exchange,
        markets: dict[str, TradeMarket],
        exchange_name: ExchangeName,
        issues: list[str],
    ) -> dict[str, Decimal]:
        now = datetime.now(timezone.utc)
        cached = self._funding_cache.get(exchange_name)
        if cached and (now - cached[0]).total_seconds() < 60:
            return cached[1]
        if not exchange.has.get("fetchFundingRates"):
            issues.append(f"{exchange_name}: 不支持批量 funding rates")
            return {}
        try:
            raw = exchange.fetch_funding_rates([market.ccxt_symbol for market in markets.values()])
        except Exception:
            try:
                raw = exchange.fetch_funding_rates()
            except Exception as exc:  # noqa: BLE001
                issues.append(f"{exchange_name}: funding rates 读取失败 {str(exc)[:180]}")
                return {}
        rates = self._normalize_funding_rates(raw)
        self._funding_cache[exchange_name] = (now, rates)
        return rates

    def _normalize_funding_rates(self, raw) -> dict[str, Decimal]:
        items = raw.values() if isinstance(raw, dict) else raw
        rates: dict[str, Decimal] = {}
        for item in items:
            symbol = item.get("symbol")
            if symbol:
                rates[normalize_ccxt_symbol(symbol)] = decimal_from(item.get("fundingRate") or item.get("nextFundingRate"))
        return rates

    def _exchange_opportunities(self, data: CashCarryExchangeData, settings: BotSettings) -> list[CashCarryOpportunity]:
        opportunities: list[CashCarryOpportunity] = []
        for symbol in sorted(set(data.spot_markets) & set(data.swap_markets)):
            if _symbol_blacklisted(symbol, settings.symbol_blacklist):
                continue
            item = self._build_opportunity(symbol, data, settings)
            if item:
                opportunities.append(item)
        return self._attach_depth_estimates(opportunities, data, settings)

    def _build_opportunity(
        self,
        symbol: str,
        data: CashCarryExchangeData,
        settings: BotSettings,
    ) -> CashCarryOpportunity | None:
        spot_ticker = data.spot_tickers.get(symbol)
        swap_ticker = data.swap_tickers.get(symbol)
        if not spot_ticker or not swap_ticker:
            return None
        spot_price = decimal_from(spot_ticker.get("ask"))
        perp_price = decimal_from(swap_ticker.get("bid"))
        if spot_price <= 0 or perp_price <= 0:
            return None
        basis_pct = calculate_spread_pct(spot_price, perp_price)
        funding_rate = data.funding_rates.get(symbol, Decimal("0"))
        spot_volume = quote_volume(spot_ticker)
        perp_volume = quote_volume(swap_ticker)
        spot_fee = data.spot_markets[symbol].taker_fee or FEE_RATES[data.exchange]
        swap_fee = data.swap_markets[symbol].taker_fee or FEE_RATES[data.exchange]
        fees = settings.order_notional_usdt * (spot_fee + swap_fee) * Decimal("2")
        basis_profit = convergence_basis_profit(settings, basis_pct)
        funding_income = settings.order_notional_usdt * funding_rate
        estimated_net_profit = estimated_entry_net_profit(settings, basis_pct, funding_rate, fees)
        reasons = self._blocked_reasons(basis_pct, funding_rate, spot_volume, perp_volume, estimated_net_profit, settings)
        reasons.extend(entry_basis_risk_reasons(basis_pct, settings))
        reasons.extend(self.history_quality.entry_net_reasons(estimated_net_profit, settings))
        reasons.extend(self.history_quality.blocked_reasons(data.exchange, symbol, settings))
        reasons.extend(local_identity_reasons(data.exchange.value, data.swap_markets[symbol].asset, data.spot_markets[symbol].asset))
        if self._pre_market_spot_transfer_closed(data.swap_markets[symbol], data.spot_markets[symbol]):
            reasons.append("预上市合约且现货充提均关闭，禁止自动开仓")
        return CashCarryOpportunity(
            exchange=data.exchange,
            symbol=symbol,
            spot_price=q(spot_price),
            perp_price=q(perp_price),
            basis_pct=q(basis_pct),
            funding_rate_pct=q(funding_rate * Decimal("100")),
            quantity=q(settings.order_notional_usdt / spot_price, "0.000001"),
            spot_volume_24h_usdt=q(spot_volume, "0.01"),
            perp_volume_24h_usdt=q(perp_volume, "0.01"),
            estimated_basis_profit=q(basis_profit),
            estimated_funding_income=q(funding_income),
            estimated_open_close_fee=q(fees),
            estimated_net_profit=q(estimated_net_profit),
            notional_usdt=q(settings.order_notional_usdt, "0.01"),
            margin_required_usdt=q(settings.order_notional_usdt / settings.default_leverage if settings.default_leverage > 0 else settings.order_notional_usdt, "0.01"),
            leverage=settings.default_leverage,
            blocked_reasons=reasons,
            data_source=DataSource.LIVE,
            updated_at=datetime.now(timezone.utc),
        )

    def _blocked_reasons(
        self,
        basis_pct: Decimal,
        funding_rate: Decimal,
        spot_volume: Decimal,
        perp_volume: Decimal,
        estimated_net_profit: Decimal,
        settings: BotSettings,
    ) -> list[str]:
        reasons: list[str] = []
        if basis_pct < settings.cash_carry_min_basis_pct and not self.history_quality.bootstrap_basis_allows(basis_pct, estimated_net_profit, settings):
            reasons.append(f"合约溢价未达 {settings.cash_carry_min_basis_pct}%")
        funding_rate_pct = funding_rate * Decimal("100")
        if funding_rate <= 0:
            reasons.append("资金费率不是正数，空头不能收资金费")
        elif funding_rate_pct < settings.cash_carry_min_funding_rate_pct:
            reasons.append(f"资金费率低于 {settings.cash_carry_min_funding_rate_pct}%")
        min_volume = min(spot_volume, perp_volume)
        if min_volume < settings.cash_carry_min_volume_usdt:
            reasons.append(f"现货/合约最低24h成交量低于 {settings.cash_carry_min_volume_usdt}U")
        return reasons

    def _attach_depth_estimates(
        self,
        items: list[CashCarryOpportunity],
        data: CashCarryExchangeData,
        settings: BotSettings,
    ) -> list[CashCarryOpportunity]:
        if not data.spot_exchange or not data.swap_exchange:
            return items
        ready = [item for item in items if not item.blocked_reasons]
        top_keys = {
            item.symbol
            for item in sorted(ready, key=lambda row: self._opportunity_sort_key(row, settings))[:10]
        }
        if not top_keys:
            return items
        return [
            self._with_depth_estimate(item, data, settings) if item.symbol in top_keys else item
            for item in items
        ]

    def _with_depth_estimate(
        self,
        item: CashCarryOpportunity,
        data: CashCarryExchangeData,
        settings: BotSettings,
    ) -> CashCarryOpportunity:
        spot_market = data.spot_markets.get(item.symbol)
        swap_market = data.swap_markets.get(item.symbol)
        if not spot_market or not swap_market:
            return item
        estimate = estimate_max_safe_notional(
            data.spot_exchange,
            data.swap_exchange,
            spot_market.ccxt_symbol,
            swap_market.ccxt_symbol,
            settings,
            spot_market.taker_fee or FEE_RATES[data.exchange],
            swap_market.taker_fee or FEE_RATES[data.exchange],
            data.funding_rates.get(item.symbol, Decimal("0")),
        )
        if estimate is None:
            return item
        safe_notional = q(estimate, "0.01")
        updates: dict[str, object] = {"max_safe_notional_usdt": safe_notional}
        if estimate < settings.order_notional_usdt:
            updates["blocked_reasons"] = [
                *item.blocked_reasons,
                f"盘口深度不足，最大安全本金 {safe_notional}U < 单笔 {q(settings.order_notional_usdt, '0.01')}U",
            ]
        return item.model_copy(update=updates)

    def _candidate_sort_key(self, item: CashCarryOpportunity, settings: BotSettings) -> tuple[int, int, Decimal, Decimal, Decimal, Decimal]:
        return cash_carry_candidate_sort_key(
            settings,
            item.blocked_reasons,
            item.basis_pct,
            item.estimated_net_profit,
            self._quality_score(item, settings),
        )

    def _opportunity_sort_key(self, item: CashCarryOpportunity, settings: BotSettings) -> tuple[Decimal, Decimal]:
        return (-self._quality_score(item, settings), -item.estimated_net_profit)

    def _quality_score(self, item: CashCarryOpportunity, settings: BotSettings) -> Decimal:
        return cash_carry_quality_score(
            settings,
            item.basis_pct,
            item.funding_rate_pct / Decimal("100"),
            min(item.spot_volume_24h_usdt, item.perp_volume_24h_usdt),
            item.estimated_net_profit,
            item.max_safe_notional_usdt,
        )

    def _is_pre_market(self, market: dict[str, Any]) -> bool:
        info = market.get("info") if isinstance(market.get("info"), dict) else {}
        return bool(market.get("isPreMarket") or market.get("preMarket") or info.get("is_pre_market") or info.get("isPreMarket"))

    def _pre_market_spot_transfer_closed(self, swap_market: TradeMarket, spot_market: TradeMarket) -> bool:
        return (
            swap_market.is_pre_market
            and spot_market.deposit_enabled is False
            and spot_market.withdraw_enabled is False
        )


def _symbol_blacklisted(symbol: str, blacklist: list[str]) -> bool:
    normalized_symbol = _normalize_blacklist_token(symbol)
    base_asset = normalized_symbol.removesuffix("USDT")
    return any(_normalize_blacklist_token(item) in {normalized_symbol, base_asset} for item in blacklist)


def _normalize_blacklist_token(value: str) -> str:
    return value.upper().replace("/", "").replace(":", "").replace("-", "").strip()
