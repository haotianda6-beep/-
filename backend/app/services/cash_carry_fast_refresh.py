from datetime import datetime, timezone
from decimal import Decimal

from app.core.market_math import q
from app.core.models import BotSettings, CashCarryOpportunity, ExchangeName
from app.core.pnl import calculate_spread_pct
from app.services.cash_carry_history_quality import CashCarryHistoryQuality
from app.services.cash_carry_quality import (
    cash_carry_candidate_sort_key,
    cash_carry_quality_score,
    convergence_basis_profit,
    entry_basis_risk_reasons,
    estimated_entry_net_profit,
)
from app.services.live_market_types import CashCarryScan
from app.services.live_read import decimal_from
from app.services.market_format import quote_volume
from app.services.cash_carry_scope import CASH_CARRY_INTERNAL_CANDIDATE_LIMIT
from app.services.ws_ticker_cache import WSTickerCache


class CashCarryFastRefresher:
    def __init__(self, ticker_cache: WSTickerCache, history_quality: CashCarryHistoryQuality | None = None) -> None:
        self.ticker_cache = ticker_cache
        self.history_quality = history_quality or CashCarryHistoryQuality()

    def refresh(self, scan: CashCarryScan, settings: BotSettings) -> CashCarryScan:
        items = [item for item in self._unique_items(scan) if not _symbol_blacklisted(item.symbol, settings.symbol_blacklist)]
        if not items:
            return CashCarryScan(issues=scan.issues)
        refreshed = [self._refresh_one(item, settings) for item in items]
        opportunities = [item for item in refreshed if not item.blocked_reasons]
        candidates = sorted(refreshed, key=lambda item: self._candidate_sort_key(item, settings))[:CASH_CARRY_INTERNAL_CANDIDATE_LIMIT]
        return CashCarryScan(
            opportunities=sorted(opportunities, key=lambda item: item.estimated_net_profit, reverse=True),
            candidates=candidates,
            issues=scan.issues,
        )

    def _unique_items(self, scan: CashCarryScan) -> list[CashCarryOpportunity]:
        seen: set[tuple[ExchangeName, str]] = set()
        items = []
        for item in [*scan.opportunities, *scan.candidates]:
            key = (ExchangeName(item.exchange), item.symbol)
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
        return items

    def _refresh_one(self, item: CashCarryOpportunity, settings: BotSettings) -> CashCarryOpportunity:
        exchange = ExchangeName(item.exchange)
        spot_ticker = self.ticker_cache.get(exchange, "spot", item.symbol)
        swap_ticker = self.ticker_cache.get(exchange, "swap", item.symbol)
        if not spot_ticker or not swap_ticker:
            return item
        return self._refresh_forward(item, spot_ticker, swap_ticker, settings)

    def _refresh_forward(
        self,
        item: CashCarryOpportunity,
        spot_ticker: dict,
        swap_ticker: dict,
        settings: BotSettings,
    ) -> CashCarryOpportunity:
        spot_price = self._price(spot_ticker, ("ask", "last", "close"), item.spot_price)
        perp_price = self._price(swap_ticker, ("bid", "last", "close"), item.perp_price)
        if spot_price <= 0 or perp_price <= 0:
            return item
        basis_pct = calculate_spread_pct(spot_price, perp_price)
        funding_rate = item.funding_rate_pct / Decimal("100")
        spot_volume = quote_volume(spot_ticker) or item.spot_volume_24h_usdt
        perp_volume = quote_volume(swap_ticker) or item.perp_volume_24h_usdt
        basis_profit = convergence_basis_profit(settings, basis_pct)
        funding_income = settings.order_notional_usdt * funding_rate
        net = estimated_entry_net_profit(settings, basis_pct, funding_rate, item.estimated_open_close_fee)
        reasons = self._forward_reasons(item.blocked_reasons, ExchangeName(item.exchange), basis_pct, funding_rate, spot_volume, perp_volume, net, settings)
        return item.model_copy(update={
            "spot_price": q(spot_price),
            "perp_price": q(perp_price),
            "basis_pct": q(basis_pct),
            "quantity": q(settings.order_notional_usdt / spot_price, "0.000001"),
            "spot_volume_24h_usdt": q(spot_volume, "0.01"),
            "perp_volume_24h_usdt": q(perp_volume, "0.01"),
            "estimated_basis_profit": q(basis_profit),
            "estimated_funding_income": q(funding_income),
            "estimated_net_profit": q(net),
            "notional_usdt": q(settings.order_notional_usdt, "0.01"),
            "margin_required_usdt": q(settings.order_notional_usdt / settings.default_leverage if settings.default_leverage > 0 else settings.order_notional_usdt, "0.01"),
            "leverage": settings.default_leverage,
            "blocked_reasons": self._dedupe(reasons),
            "updated_at": datetime.now(timezone.utc),
        })

    def _price(self, ticker: dict, keys: tuple[str, ...], fallback: Decimal) -> Decimal:
        for key in keys:
            price = decimal_from(ticker.get(key))
            if price > 0:
                return price
        return fallback

    def _forward_reasons(
        self,
        current: list[str],
        exchange: ExchangeName,
        basis_pct: Decimal,
        funding_rate: Decimal,
        spot_volume: Decimal,
        perp_volume: Decimal,
        estimated_net_profit: Decimal,
        settings: BotSettings,
    ) -> list[str]:
        reasons = self._preserved(
            current,
            (
                "合约溢价未达",
                "开仓基差异常过高",
                "资金费率低于",
                "资金费率不是正数",
                "现货/合约最低24h成交量低于",
                "回归到平仓线后的净利预估",
                "V3冷启动净利预估",
                "V3频率调节净利预估",
                "V2历史胜率保护",
                "V3历史胜率保护",
                "信号持续不足",
                "基差波动过大",
                "基差分位样本不足",
                "基差分位不足",
            ),
        )
        if basis_pct < settings.cash_carry_min_basis_pct and not self.history_quality.bootstrap_basis_allows(basis_pct, estimated_net_profit, settings, exchange=exchange):
            reasons.append(f"合约溢价未达 {settings.cash_carry_min_basis_pct}%")
        reasons.extend(entry_basis_risk_reasons(basis_pct, settings))
        if funding_rate <= 0:
            reasons.append("资金费率不是正数，空头不能收资金费")
        elif funding_rate * Decimal("100") < settings.cash_carry_min_funding_rate_pct:
            reasons.append(f"资金费率低于 {settings.cash_carry_min_funding_rate_pct}%")
        if min(spot_volume, perp_volume) < settings.cash_carry_min_volume_usdt:
            reasons.append(f"现货/合约最低24h成交量低于 {settings.cash_carry_min_volume_usdt}U")
        reasons.extend(self.history_quality.entry_net_reasons(estimated_net_profit, settings, exchange=exchange))
        return self._dedupe(reasons)

    def _preserved(self, current: list[str], dynamic_prefixes: tuple[str, ...]) -> list[str]:
        return [reason for reason in current if not any(reason.startswith(prefix) for prefix in dynamic_prefixes)]

    def _dedupe(self, reasons: list[str]) -> list[str]:
        result = []
        seen = set()
        for reason in reasons:
            if reason in seen:
                continue
            seen.add(reason)
            result.append(reason)
        return result

    def _candidate_sort_key(self, item: CashCarryOpportunity, settings: BotSettings):
        quality = cash_carry_quality_score(
            settings,
            item.basis_pct,
            item.funding_rate_pct / Decimal("100"),
            min(item.spot_volume_24h_usdt, item.perp_volume_24h_usdt),
            item.estimated_net_profit,
            item.max_safe_notional_usdt,
        )
        return cash_carry_candidate_sort_key(settings, item.blocked_reasons, item.basis_pct, item.estimated_net_profit, quality)


def _symbol_blacklisted(symbol: str, blacklist: list[str]) -> bool:
    normalized_symbol = _normalize_blacklist_token(symbol)
    base_asset = normalized_symbol.removesuffix("USDT")
    return any(_normalize_blacklist_token(item) in {normalized_symbol, base_asset} for item in blacklist)


def _normalize_blacklist_token(value: str) -> str:
    return value.upper().replace("/", "").replace(":", "").replace("-", "").strip()
