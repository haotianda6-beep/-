from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.config import Settings
from app.models import MarketQuote, utc_now_ms
from app.storage import Storage


@dataclass(frozen=True)
class RiskResult:
    ok: bool
    reason: str = ""


class RiskManager:
    def __init__(self, settings: Settings, storage: Storage | None = None) -> None:
        self.settings = settings
        self.storage = storage

    def live_ready(self, binance_ready: bool, mt4_connected: bool, maker_fee_loaded: bool) -> RiskResult:
        if not self.settings.live_trading or self.settings.paper_mode:
            return RiskResult(False, "dry-run/demo mode")
        if not self.settings.binance_api_key or not self.settings.binance_api_secret:
            return RiskResult(False, "Binance API key/secret missing")
        if not mt4_connected:
            return RiskResult(False, "MT4 bridge not connected")
        if not binance_ready:
            return RiskResult(False, "Binance client not ready")
        if not maker_fee_loaded:
            return RiskResult(False, "Binance maker fee missing")
        if self.storage and self.storage.daily_pnl() <= -self.settings.daily_loss_limit_usdt:
            return RiskResult(False, "daily loss limit reached")
        return RiskResult(True)

    def quote_fresh(self, quote: MarketQuote | None) -> RiskResult:
        if quote is None:
            return RiskResult(False, "quote missing")
        age = utc_now_ms() - quote.timestamp_ms
        if age > self.settings.max_quote_age_ms:
            return RiskResult(False, f"quote stale {age}ms")
        return RiskResult(True)

    def mt4_buy_price_ok(self, fill_price: Decimal, mt4_ask: Decimal) -> RiskResult:
        max_price = fill_price - self.settings.min_locked_edge
        if mt4_ask > max_price:
            return RiskResult(False, f"MT4 buy ask {mt4_ask} above max {max_price}")
        return RiskResult(True)

    def mt4_sell_price_ok(self, fill_price: Decimal, mt4_bid: Decimal) -> RiskResult:
        min_price = fill_price + self.settings.min_locked_edge
        if mt4_bid < min_price:
            return RiskResult(False, f"MT4 sell bid {mt4_bid} below min {min_price}")
        return RiskResult(True)

