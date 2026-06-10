from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal

from app.core.market_math import FEE_RATES, q
from app.core.models import BotSettings, CashCarryOpportunity, DataSource, ExchangeName
from app.services.asset_identity import local_identity_reasons
from app.services.borrow_pool_blocklist import active_borrow_pool_block
from app.services.borrow_checker import BorrowCheck, BorrowChecker
from app.services.cash_carry_scanner import CashCarryExchangeData, CashCarryScanner
from app.services.live_market_types import CashCarryScan
from app.services.live_read import decimal_from
from app.services.market_format import quote_volume


class ReverseCashCarryScanner(CashCarryScanner):
    def __init__(self, borrow_checker: BorrowChecker | None = None) -> None:
        super().__init__()
        self.borrow_checker = borrow_checker or BorrowChecker()

    def scan(self, settings: BotSettings) -> CashCarryScan:
        if not settings.reverse_cash_carry_enabled:
            return CashCarryScan()
        exchanges = [exchange for exchange in ExchangeName if exchange not in set(settings.exchange_blacklist)]
        with ThreadPoolExecutor(max_workers=max(1, len(exchanges))) as executor:
            data = list(executor.map(self._load_exchange_data, exchanges))
        checked = [
            item
            for exchange_data in data
            for item in self._exchange_opportunities(exchange_data, settings)
        ]
        opportunities = [item for item in checked if not item.blocked_reasons]
        candidates = sorted(checked, key=lambda item: (len(item.blocked_reasons), -item.estimated_net_profit))[:50]
        return CashCarryScan(
            opportunities=sorted(opportunities, key=lambda item: item.estimated_net_profit, reverse=True),
            candidates=candidates,
            issues=[issue for item in data for issue in item.issues],
        )

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
        spot_price = decimal_from(spot_ticker.get("bid"))
        perp_price = decimal_from(swap_ticker.get("ask"))
        if spot_price <= 0 or perp_price <= 0:
            return None
        discount_pct = (spot_price - perp_price) / spot_price * Decimal("100")
        funding_rate = data.funding_rates.get(symbol, Decimal("0"))
        spot_volume = quote_volume(spot_ticker)
        perp_volume = quote_volume(swap_ticker)
        spot_fee = data.spot_markets[symbol].taker_fee or FEE_RATES[data.exchange]
        swap_fee = data.swap_markets[symbol].taker_fee or FEE_RATES[data.exchange]
        fees = settings.order_notional_usdt * (spot_fee + swap_fee) * Decimal("2")
        basis_profit = settings.order_notional_usdt * discount_pct / Decimal("100")
        funding_income = -settings.order_notional_usdt * funding_rate
        quantity = settings.order_notional_usdt / spot_price
        reasons = self._blocked_reasons(discount_pct, funding_rate, spot_volume, perp_volume, settings)
        reasons.extend(local_identity_reasons(data.exchange.value, data.swap_markets[symbol].asset, data.spot_markets[symbol].asset))
        if self._pre_market_spot_transfer_closed(data.swap_markets[symbol], data.spot_markets[symbol]):
            reasons.append("预上市合约且现货充提均关闭，禁止自动开仓")
        borrow_block = active_borrow_pool_block(data.exchange, symbol)
        if borrow_block:
            reasons.append(borrow_block.reason)
        borrow = BorrowCheck(status="blocked", available_qty=Decimal("0")) if borrow_block else BorrowCheck(status="not_required")
        if not reasons:
            borrow = self.borrow_checker.check(
                data.exchange,
                data.spot_markets[symbol].asset.base,
                quantity,
                spot_price,
                settings.reverse_cash_carry_borrow_hold_hours,
            )
        reasons.extend(borrow.blocked_reasons)
        net_profit = basis_profit + funding_income - fees - (borrow.estimated_cost_usdt or Decimal("0"))
        if borrow.status == "ok" and net_profit <= 0:
            reasons.append("扣除借币成本后净利不为正")
        return CashCarryOpportunity(
            exchange=data.exchange,
            symbol=symbol,
            spot_price=q(spot_price),
            perp_price=q(perp_price),
            basis_pct=q(discount_pct),
            funding_rate_pct=q(funding_rate * Decimal("100")),
            quantity=q(quantity, "0.000001"),
            spot_volume_24h_usdt=q(spot_volume, "0.01"),
            perp_volume_24h_usdt=q(perp_volume, "0.01"),
            estimated_basis_profit=q(basis_profit),
            estimated_funding_income=q(funding_income),
            estimated_open_close_fee=q(fees),
            estimated_borrow_cost=q(borrow.estimated_cost_usdt) if borrow.estimated_cost_usdt is not None else None,
            estimated_net_profit=q(net_profit),
            notional_usdt=q(settings.order_notional_usdt, "0.01"),
            margin_required_usdt=q(settings.order_notional_usdt / settings.default_leverage if settings.default_leverage > 0 else settings.order_notional_usdt, "0.01"),
            leverage=settings.default_leverage,
            borrow_check_status=borrow.status,
            borrow_available_qty=q(borrow.available_qty, "0.000001") if borrow.available_qty is not None else None,
            borrow_daily_rate_pct=q(borrow.daily_rate * Decimal("100")) if borrow.daily_rate is not None else None,
            borrow_rate_period_hours=q(borrow.rate_period_hours) if borrow.rate_period_hours is not None else None,
            borrow_term=borrow.term,
            borrow_risk_tags=borrow.risk_tags,
            blocked_reasons=reasons,
            data_source=DataSource.LIVE,
            updated_at=datetime.now(timezone.utc),
        )

    def _blocked_reasons(
        self,
        discount_pct: Decimal,
        funding_rate: Decimal,
        spot_volume: Decimal,
        perp_volume: Decimal,
        settings: BotSettings,
    ) -> list[str]:
        reasons: list[str] = []
        if discount_pct < settings.reverse_cash_carry_min_discount_pct:
            reasons.append(f"合约折价未达 {settings.reverse_cash_carry_min_discount_pct}%")
        funding_rate_pct = abs(funding_rate * Decimal("100"))
        if funding_rate >= 0:
            reasons.append("资金费率不是负数，多头不能收资金费")
        elif funding_rate_pct < settings.reverse_cash_carry_min_funding_rate_pct:
            reasons.append(f"负资金费率低于 {settings.reverse_cash_carry_min_funding_rate_pct}%")
        min_volume = min(spot_volume, perp_volume)
        if min_volume < settings.reverse_cash_carry_min_volume_usdt:
            reasons.append(f"现货/合约最低24h成交量低于 {settings.reverse_cash_carry_min_volume_usdt}U")
        return reasons

    def _depth_side(self) -> str:
        return "reverse"
