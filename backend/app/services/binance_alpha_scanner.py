from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.core.models import AlphaCarryOpportunity, BotSettings, DataSource
from app.core.pnl import calculate_spread_pct


ALPHA_TOKEN_LIST_URL = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
ALPHA_EXCHANGE_INFO_URL = "https://www.binance.com/bapi/defi/v1/public/alpha-trade/get-exchange-info"
FUTURES_BOOK_URL = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"
FUTURES_24H_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
FUTURES_FUNDING_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
MAX_REASONABLE_ALPHA_BASIS_PCT = Decimal("200")


@dataclass
class AlphaAlertScan:
    opportunities: list[AlphaCarryOpportunity] = field(default_factory=list)
    candidates: list[AlphaCarryOpportunity] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AlphaToken:
    base: str
    symbol: str
    alpha_id: str
    alpha_trade_symbol: str
    name: str
    chain_name: str
    contract_address: str
    price: Decimal
    volume_24h: Decimal
    offline: bool
    fully_delisted: bool
    offsell: bool
    duplicate: bool


class BinanceAlphaScanner:
    def scan(self, settings: BotSettings) -> AlphaAlertScan:
        if not settings.alpha_alert_enabled:
            return AlphaAlertScan()
        try:
            raw_tokens, raw_exchange_info, futures = self._load()
            tokens = self._tradable_tokens(raw_tokens, raw_exchange_info)
            rows = self._build_rows(tokens, futures, settings)
        except Exception as exc:  # noqa: BLE001 - external public endpoints may fail.
            return AlphaAlertScan(issues=[f"币安 Alpha 行情读取失败：{str(exc)[:180]}"])
        opportunities = [item for item in rows if not item.blocked_reasons]
        candidates = sorted(rows, key=lambda item: (len(item.blocked_reasons), -item.estimated_net_profit))[:80]
        return AlphaAlertScan(
            opportunities=sorted(opportunities, key=lambda item: item.estimated_net_profit, reverse=True),
            candidates=candidates,
        )

    def _load(self) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, dict[str, Any]]]:
        with httpx.Client(timeout=15.0, headers={"User-Agent": "perp-arb-alpha-alert/1.0"}) as client:
            tokens = self._json_data(client, ALPHA_TOKEN_LIST_URL)
            exchange_info = self._json_data(client, ALPHA_EXCHANGE_INFO_URL)
            book = client.get(FUTURES_BOOK_URL)
            book.raise_for_status()
            tickers = client.get(FUTURES_24H_URL)
            tickers.raise_for_status()
            funding = client.get(FUTURES_FUNDING_URL)
            funding.raise_for_status()
            futures = self._futures_map(book.json(), tickers.json(), funding.json())
        return tokens if isinstance(tokens, list) else [], exchange_info if isinstance(exchange_info, dict) else {}, futures

    def _json_data(self, client: httpx.Client, url: str) -> Any:
        response = client.get(url)
        response.raise_for_status()
        body = response.json()
        if body.get("success") is False:
            raise RuntimeError(body.get("message") or "Alpha 接口返回失败")
        return body.get("data")

    def _futures_map(self, book_raw: Any, ticker_raw: Any, funding_raw: Any) -> dict[str, dict[str, Any]]:
        tickers = {item.get("symbol"): item for item in ticker_raw if isinstance(item, dict)}
        funding = {item.get("symbol"): item for item in funding_raw if isinstance(item, dict)}
        result: dict[str, dict[str, Any]] = {}
        for item in book_raw if isinstance(book_raw, list) else []:
            symbol = str(item.get("symbol") or "")
            if not symbol.endswith("USDT"):
                continue
            base = symbol[:-4]
            result[base] = {
                "symbol": symbol,
                "bid": decimal_from(item.get("bidPrice")),
                "ask": decimal_from(item.get("askPrice")),
                "volume": decimal_from(tickers.get(symbol, {}).get("quoteVolume")),
                "funding_rate": decimal_from(funding.get(symbol, {}).get("lastFundingRate")) * Decimal("100"),
            }
        return result

    def _tradable_tokens(self, raw_tokens: list[dict[str, Any]], raw_exchange_info: dict[str, Any]) -> list[AlphaToken]:
        tradable_alpha_ids = {
            str(item.get("baseAsset"))
            for item in raw_exchange_info.get("symbols", [])
            if item.get("quoteAsset") == "USDT" and item.get("status") == "TRADING"
        }
        counts: dict[str, int] = {}
        for item in raw_tokens:
            base = self._match_base(item)
            if item.get("alphaId") in tradable_alpha_ids and base:
                counts[base] = counts.get(base, 0) + 1
        tokens: list[AlphaToken] = []
        for item in raw_tokens:
            alpha_id = str(item.get("alphaId") or "")
            if alpha_id not in tradable_alpha_ids:
                continue
            base = self._match_base(item)
            if not base:
                continue
            tokens.append(
                AlphaToken(
                    base=base,
                    symbol=str(item.get("symbol") or base).upper(),
                    alpha_id=alpha_id,
                    alpha_trade_symbol=f"{alpha_id}USDT",
                    name=str(item.get("name") or ""),
                    chain_name=str(item.get("chainName") or ""),
                    contract_address=str(item.get("contractAddress") or ""),
                    price=decimal_from(item.get("price")),
                    volume_24h=decimal_from(item.get("volume24h")),
                    offline=bool(item.get("offline")),
                    fully_delisted=bool(item.get("fullyDelisted")),
                    offsell=bool(item.get("offsell")),
                    duplicate=counts.get(base, 0) > 1,
                )
            )
        return tokens

    def _match_base(self, item: dict[str, Any]) -> str:
        cex_name = str(item.get("cexCoinName") or "").strip().upper()
        symbol = str(item.get("symbol") or "").strip().upper()
        return cex_name or symbol

    def _build_rows(
        self,
        tokens: list[AlphaToken],
        futures: dict[str, dict[str, Any]],
        settings: BotSettings,
    ) -> list[AlphaCarryOpportunity]:
        rows: list[AlphaCarryOpportunity] = []
        now = datetime.now(timezone.utc)
        for token in tokens:
            future = futures.get(token.base)
            if not future:
                continue
            row = self._row(token, future, settings, now)
            if row:
                rows.append(row)
        return rows

    def _row(
        self,
        token: AlphaToken,
        future: dict[str, Any],
        settings: BotSettings,
        now: datetime,
    ) -> AlphaCarryOpportunity | None:
        alpha_price = token.price
        perp_bid = decimal_from(future.get("bid"))
        perp_ask = decimal_from(future.get("ask"))
        if alpha_price <= 0 or perp_bid <= 0:
            return None
        basis_pct = calculate_spread_pct(alpha_price, perp_bid)
        if basis_pct > MAX_REASONABLE_ALPHA_BASIS_PCT:
            return None
        funding_rate_pct = decimal_from(future.get("funding_rate"))
        basis_profit = settings.alpha_alert_notional_usdt * basis_pct / Decimal("100")
        funding_income = settings.alpha_alert_notional_usdt * funding_rate_pct / Decimal("100")
        fee_reserve = settings.alpha_alert_notional_usdt * settings.alpha_alert_fee_reserve_pct / Decimal("100")
        reasons = self._blocked_reasons(token, future, basis_pct, funding_rate_pct, settings)
        return AlphaCarryOpportunity(
            symbol=f"{token.base}USDT",
            alpha_symbol=token.symbol,
            alpha_trade_symbol=token.alpha_trade_symbol,
            alpha_id=token.alpha_id,
            alpha_name=token.name,
            chain_name=token.chain_name,
            contract_address=token.contract_address,
            perp_symbol=str(future.get("symbol") or f"{token.base}USDT"),
            alpha_price=alpha_price,
            perp_bid_price=perp_bid,
            perp_ask_price=perp_ask,
            basis_pct=basis_pct,
            funding_rate_pct=funding_rate_pct,
            alpha_volume_24h_usdt=token.volume_24h,
            perp_volume_24h_usdt=decimal_from(future.get("volume")),
            notional_usdt=settings.alpha_alert_notional_usdt,
            estimated_basis_profit=basis_profit,
            estimated_funding_income=funding_income,
            estimated_fee_reserve=fee_reserve,
            estimated_net_profit=basis_profit + funding_income - fee_reserve,
            blocked_reasons=reasons,
            data_source=DataSource.LIVE,
            updated_at=now,
        )

    def _blocked_reasons(
        self,
        token: AlphaToken,
        future: dict[str, Any],
        basis_pct: Decimal,
        funding_rate_pct: Decimal,
        settings: BotSettings,
    ) -> list[str]:
        reasons: list[str] = []
        if token.duplicate:
            reasons.append("Alpha 同名多链/多个 AlphaId，需人工确认不是同名不同币")
        if token.offline or token.fully_delisted or token.offsell:
            reasons.append("Alpha 显示下架或限制交易")
        if basis_pct < settings.alpha_alert_min_basis_pct:
            reasons.append(f"合约溢价未达阈值 {basis_pct:.4f}% < {settings.alpha_alert_min_basis_pct}%")
        if funding_rate_pct <= settings.alpha_alert_min_funding_rate_pct:
            reasons.append(f"资金费率不足 {funding_rate_pct:.4f}% <= {settings.alpha_alert_min_funding_rate_pct}%")
        min_volume = min(token.volume_24h, decimal_from(future.get("volume")))
        if min_volume < settings.alpha_alert_min_volume_usdt:
            reasons.append(f"最低24h成交量不足 {min_volume:.0f}U < {settings.alpha_alert_min_volume_usdt}U")
        return reasons


def decimal_from(value: Any, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(default)
