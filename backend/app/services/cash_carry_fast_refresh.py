from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from app.core.market_math import q
from app.core.models import BotSettings, CashCarryOpportunity, ExchangeName
from app.core.pnl import calculate_spread_pct
from app.services.borrow_pool_blocklist import active_borrow_pool_block
from app.services.live_market_types import CashCarryScan
from app.services.live_read import decimal_from
from app.services.market_format import quote_volume
from app.services.ws_ticker_cache import WSTickerCache


StrategySide = Literal["forward", "reverse"]


class CashCarryFastRefresher:
    def __init__(self, ticker_cache: WSTickerCache, side: StrategySide) -> None:
        self.ticker_cache = ticker_cache
        self.side = side

    def refresh(self, scan: CashCarryScan, settings: BotSettings) -> CashCarryScan:
        items = self._unique_items(scan)
        if not items:
            return scan
        refreshed = [self._refresh_one(item, settings) for item in items]
        opportunities = [item for item in refreshed if not item.blocked_reasons]
        candidates = sorted(refreshed, key=lambda item: (len(item.blocked_reasons), -item.estimated_net_profit))[:50]
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
            if self.side == "reverse":
                return self._refresh_reverse_static_reasons(item)
            return item
        if self.side == "reverse":
            return self._refresh_reverse(item, spot_ticker, swap_ticker, settings)
        return self._refresh_forward(item, spot_ticker, swap_ticker, settings)

    def _refresh_reverse_static_reasons(self, item: CashCarryOpportunity) -> CashCarryOpportunity:
        reasons = self._preserved(item.blocked_reasons, ("借币资金池不足", "实盘借币失败"))
        borrow_block = active_borrow_pool_block(ExchangeName(item.exchange), item.symbol)
        updates = {
            "blocked_reasons": reasons,
            "updated_at": datetime.now(timezone.utc),
        }
        if borrow_block:
            reasons.append(borrow_block.reason)
            updates.update({
                "borrow_check_status": "blocked",
                "borrow_available_qty": q(borrow_block.available_qty, "0.000001"),
            })
        updates["blocked_reasons"] = self._dedupe(reasons)
        if reasons == item.blocked_reasons:
            return item
        return item.model_copy(update=updates)

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
        basis_profit = settings.order_notional_usdt * basis_pct / Decimal("100")
        funding_income = settings.order_notional_usdt * funding_rate
        net = basis_profit + funding_income - item.estimated_open_close_fee
        reasons = self._forward_reasons(item.blocked_reasons, basis_pct, funding_rate, spot_volume, perp_volume, settings)
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

    def _refresh_reverse(
        self,
        item: CashCarryOpportunity,
        spot_ticker: dict,
        swap_ticker: dict,
        settings: BotSettings,
    ) -> CashCarryOpportunity:
        spot_price = self._price(spot_ticker, ("bid", "last", "close"), item.spot_price)
        perp_price = self._price(swap_ticker, ("ask", "last", "close"), item.perp_price)
        if spot_price <= 0 or perp_price <= 0:
            return item
        discount_pct = (spot_price - perp_price) / spot_price * Decimal("100")
        funding_rate = item.funding_rate_pct / Decimal("100")
        spot_volume = quote_volume(spot_ticker) or item.spot_volume_24h_usdt
        perp_volume = quote_volume(swap_ticker) or item.perp_volume_24h_usdt
        basis_profit = settings.order_notional_usdt * discount_pct / Decimal("100")
        funding_income = -settings.order_notional_usdt * funding_rate
        borrow_cost = item.estimated_borrow_cost or Decimal("0")
        net = basis_profit + funding_income - item.estimated_open_close_fee - borrow_cost
        reasons = self._reverse_reasons(item, discount_pct, funding_rate, spot_volume, perp_volume, net, settings)
        updates = {
            "spot_price": q(spot_price),
            "perp_price": q(perp_price),
            "basis_pct": q(discount_pct),
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
        }
        borrow_block = active_borrow_pool_block(ExchangeName(item.exchange), item.symbol)
        if borrow_block:
            updates.update({
                "borrow_check_status": "blocked",
                "borrow_available_qty": q(borrow_block.available_qty, "0.000001"),
            })
        return item.model_copy(update=updates)

    def _price(self, ticker: dict, keys: tuple[str, ...], fallback: Decimal) -> Decimal:
        for key in keys:
            price = decimal_from(ticker.get(key))
            if price > 0:
                return price
        return fallback

    def _forward_reasons(
        self,
        current: list[str],
        basis_pct: Decimal,
        funding_rate: Decimal,
        spot_volume: Decimal,
        perp_volume: Decimal,
        settings: BotSettings,
    ) -> list[str]:
        reasons = self._preserved(current, ("合约溢价未达", "资金费率低于", "资金费率不是正数", "现货/合约最低24h成交量低于"))
        if basis_pct < settings.cash_carry_min_basis_pct:
            reasons.append(f"合约溢价未达 {settings.cash_carry_min_basis_pct}%")
        if funding_rate <= 0:
            reasons.append("资金费率不是正数，空头不能收资金费")
        elif funding_rate * Decimal("100") < settings.cash_carry_min_funding_rate_pct:
            reasons.append(f"资金费率低于 {settings.cash_carry_min_funding_rate_pct}%")
        if min(spot_volume, perp_volume) < settings.cash_carry_min_volume_usdt:
            reasons.append(f"现货/合约最低24h成交量低于 {settings.cash_carry_min_volume_usdt}U")
        return self._dedupe(reasons)

    def _reverse_reasons(
        self,
        item: CashCarryOpportunity,
        discount_pct: Decimal,
        funding_rate: Decimal,
        spot_volume: Decimal,
        perp_volume: Decimal,
        net_profit: Decimal,
        settings: BotSettings,
    ) -> list[str]:
        current = item.blocked_reasons
        prefixes = ("合约折价未达", "负资金费率低于", "资金费率不是负数", "现货/合约最低24h成交量低于", "扣除借币成本后净利不为正", "借币资金池不足", "实盘借币失败", "交易所 API 限频")
        reasons = self._preserved(current, prefixes)
        borrow_block = active_borrow_pool_block(ExchangeName(item.exchange), item.symbol)
        if borrow_block:
            reasons.append(borrow_block.reason)
        if discount_pct < settings.reverse_cash_carry_min_discount_pct:
            reasons.append(f"合约折价未达 {settings.reverse_cash_carry_min_discount_pct}%")
        if funding_rate >= 0:
            reasons.append("资金费率不是负数，多头不能收资金费")
        elif abs(funding_rate * Decimal("100")) < settings.reverse_cash_carry_min_funding_rate_pct:
            reasons.append(f"负资金费率低于 {settings.reverse_cash_carry_min_funding_rate_pct}%")
        if min(spot_volume, perp_volume) < settings.reverse_cash_carry_min_volume_usdt:
            reasons.append(f"现货/合约最低24h成交量低于 {settings.reverse_cash_carry_min_volume_usdt}U")
        if item.borrow_check_status != "ok" and not reasons:
            reasons.append("借币校验未完成，等待全量扫描")
        if item.borrow_check_status == "ok" and net_profit <= 0:
            reasons.append("扣除借币成本后净利不为正")
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
