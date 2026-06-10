import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.credentials import CredentialStore
from app.core.market_math import FEE_RATES, q
from app.core.models import BotSettings, DataSource, ExchangeName, Mt4SpreadOpportunity
from app.services.exchange_factory import build_ccxt_exchange
from app.services.live_market_types import SWAP_EXCHANGE_IDS
from app.services.live_read import decimal_from
from app.services.market_format import normalize_ccxt_symbol, normalized_market_symbol


PROJECT_ROOT = Path(__file__).resolve().parents[3]
QUOTE_PATH = PROJECT_ROOT / "config" / "mt4_quotes.json"
SYMBOL_CONFIG_PATH = PROJECT_ROOT / "config" / "mt4_symbols.json"


DEFAULT_INSTRUMENTS: dict[str, dict[str, Any]] = {
    "XAUUSD": {"type": "commodity", "aliases": ["XAUUSDT", "GOLDUSDT", "PAXGUSDT"]},
    "XAGUSD": {"type": "commodity", "aliases": ["XAGUSDT", "SILVERUSDT"]},
    "USOIL": {"type": "commodity", "aliases": ["USOILUSDT", "WTIUSDT", "OILUSDT"]},
    "UKOIL": {"type": "commodity", "aliases": ["UKOILUSDT", "BRENTUSDT"]},
    "NATGAS": {"type": "commodity", "aliases": ["NATGASUSDT", "NGUSDT"]},
    "AAPL": {"type": "stock", "aliases": ["AAPLUSDT"]},
    "AMZN": {"type": "stock", "aliases": ["AMZNUSDT"]},
    "GOOGL": {"type": "stock", "aliases": ["GOOGLUSDT", "GOOGUSDT"]},
    "META": {"type": "stock", "aliases": ["METAUSDT"]},
    "MSFT": {"type": "stock", "aliases": ["MSFTUSDT"]},
    "NVDA": {"type": "stock", "aliases": ["NVDAUSDT"]},
    "TSLA": {"type": "stock", "aliases": ["TSLAUSDT"]},
}


class Mt4QuoteIn(BaseModel):
    token: str | None = None
    symbol: str
    bid: Decimal
    ask: Decimal
    timestamp: datetime | None = None
    instrument_type: Literal["stock", "commodity"] | None = None
    contract_size: Decimal = Decimal("1")
    lots: Decimal = Decimal("1")
    tick_value: Decimal = Decimal("1")
    tick_size: Decimal = Decimal("0.01")
    swap_long_points: Decimal = Decimal("0")
    swap_short_points: Decimal = Decimal("0")
    overnight_long_usdt: Decimal | None = None
    overnight_short_usdt: Decimal | None = None


@dataclass(frozen=True)
class Mt4Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    timestamp: datetime
    instrument_type: Literal["stock", "commodity"]
    contract_size: Decimal
    lots: Decimal
    tick_value: Decimal
    tick_size: Decimal
    overnight_long_usdt: Decimal
    overnight_short_usdt: Decimal


class Mt4QuoteStore:
    def __init__(self, path: Path = QUOTE_PATH) -> None:
        self.path = path

    def update(self, payload: Mt4QuoteIn) -> Mt4Quote:
        quote = self._quote(payload)
        state = self._read()
        state[quote.symbol] = self._serialize(quote)
        self._write(state)
        return quote

    def quotes(self, max_age_seconds: Decimal) -> list[Mt4Quote]:
        now = datetime.now(timezone.utc)
        result = []
        for item in self._read().values():
            quote = self._parse(item)
            if not quote:
                continue
            if (now - quote.timestamp).total_seconds() <= float(max_age_seconds):
                result.append(quote)
        return result

    def all_quotes(self) -> list[Mt4Quote]:
        return [quote for quote in (self._parse(item) for item in self._read().values()) if quote]

    def _quote(self, payload: Mt4QuoteIn) -> Mt4Quote:
        symbol = normalize_mt4_symbol(payload.symbol)
        instrument = instrument_info(symbol, payload.instrument_type)
        tick_size = payload.tick_size if payload.tick_size > 0 else Decimal("0.01")
        overnight_long = payload.overnight_long_usdt
        overnight_short = payload.overnight_short_usdt
        if overnight_long is None:
            overnight_long = payload.swap_long_points * payload.tick_value / tick_size * payload.lots
        if overnight_short is None:
            overnight_short = payload.swap_short_points * payload.tick_value / tick_size * payload.lots
        return Mt4Quote(
            symbol=symbol,
            bid=payload.bid,
            ask=payload.ask,
            timestamp=payload.timestamp or datetime.now(timezone.utc),
            instrument_type=instrument["type"],
            contract_size=payload.contract_size,
            lots=payload.lots,
            tick_value=payload.tick_value,
            tick_size=tick_size,
            overnight_long_usdt=overnight_long,
            overnight_short_usdt=overnight_short,
        )

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _serialize(self, quote: Mt4Quote) -> dict[str, str]:
        return {
            "symbol": quote.symbol,
            "bid": str(quote.bid),
            "ask": str(quote.ask),
            "timestamp": quote.timestamp.isoformat(),
            "instrument_type": quote.instrument_type,
            "contract_size": str(quote.contract_size),
            "lots": str(quote.lots),
            "tick_value": str(quote.tick_value),
            "tick_size": str(quote.tick_size),
            "overnight_long_usdt": str(quote.overnight_long_usdt),
            "overnight_short_usdt": str(quote.overnight_short_usdt),
        }

    def _parse(self, item: Any) -> Mt4Quote | None:
        try:
            return Mt4Quote(
                symbol=str(item["symbol"]),
                bid=Decimal(str(item["bid"])),
                ask=Decimal(str(item["ask"])),
                timestamp=datetime.fromisoformat(str(item["timestamp"]).replace("Z", "+00:00")),
                instrument_type=item.get("instrument_type", "commodity"),
                contract_size=Decimal(str(item.get("contract_size") or "1")),
                lots=Decimal(str(item.get("lots") or "1")),
                tick_value=Decimal(str(item.get("tick_value") or "1")),
                tick_size=Decimal(str(item.get("tick_size") or "0.01")),
                overnight_long_usdt=Decimal(str(item.get("overnight_long_usdt") or "0")),
                overnight_short_usdt=Decimal(str(item.get("overnight_short_usdt") or "0")),
            )
        except (KeyError, ValueError):
            return None


class Mt4SpreadScanner:
    def __init__(self, quote_store: Mt4QuoteStore | None = None) -> None:
        self.quote_store = quote_store or Mt4QuoteStore()
        self._market_cache: dict[ExchangeName, tuple[datetime, dict[str, str]]] = {}
        self._funding_cache: dict[ExchangeName, tuple[datetime, dict[str, Decimal]]] = {}

    def scan(self, settings: BotSettings) -> tuple[list[Mt4SpreadOpportunity], list[Mt4SpreadOpportunity], list[str]]:
        if not settings.mt4_spread_enabled:
            return [], [], []
        issues: list[str] = []
        quotes = self.quote_store.quotes(settings.mt4_max_quote_age_seconds)
        stale = self.quote_store.all_quotes()
        if not quotes and stale:
            issues.append("MT4 报价已过期，等待插件继续推送。")
        rows: list[Mt4SpreadOpportunity] = []
        for quote in quotes:
            for exchange in ExchangeName:
                rows.extend(self._exchange_rows(exchange, quote, settings, issues))
        opportunities = [item for item in rows if not item.blocked_reasons]
        candidates = sorted(rows, key=lambda item: (len(item.blocked_reasons), -item.estimated_net_profit))[:50]
        return sorted(opportunities, key=lambda item: item.estimated_net_profit, reverse=True), candidates, issues

    def _exchange_rows(self, exchange: ExchangeName, quote: Mt4Quote, settings: BotSettings, issues: list[str]) -> list[Mt4SpreadOpportunity]:
        try:
            instance = self._exchange(exchange)
            markets = self._markets(exchange, instance)
            symbol = self._mapped_symbol(quote.symbol, markets)
            if not symbol:
                return [self._blocked_missing_market(exchange, quote, settings)]
            ticker = instance.fetch_ticker(markets[symbol])
            funding = self._funding(exchange, instance).get(symbol, Decimal("0"))
            return [self._row(exchange, quote, symbol, ticker, funding, settings)]
        except Exception as exc:  # noqa: BLE001
            issues.append(f"{exchange}: MT4 对比合约行情读取失败 {str(exc)[:180]}")
            return []

    def _row(
        self,
        exchange: ExchangeName,
        quote: Mt4Quote,
        exchange_symbol: str,
        ticker: dict[str, Any],
        funding_rate: Decimal,
        settings: BotSettings,
    ) -> Mt4SpreadOpportunity:
        exchange_bid = decimal_from(ticker.get("bid") or ticker.get("last"))
        exchange_ask = decimal_from(ticker.get("ask") or ticker.get("last"))
        if exchange_bid <= 0 or exchange_ask <= 0 or quote.bid <= 0 or quote.ask <= 0:
            return self._blocked(exchange, quote, exchange_symbol, settings, "MT4 或交易所报价无效")
        mt4_mid = (quote.bid + quote.ask) / Decimal("2")
        exchange_mid = (exchange_bid + exchange_ask) / Decimal("2")
        if exchange_mid >= mt4_mid:
            spread_pct = (exchange_bid - quote.ask) / quote.ask * Decimal("100")
            long_venue, short_venue = "MT4", exchange.value
            exchange_funding = settings.mt4_notional_usdt * funding_rate
            mt4_overnight = settings.mt4_notional_usdt / quote.ask * quote.overnight_long_usdt
        else:
            spread_pct = (quote.bid - exchange_ask) / exchange_ask * Decimal("100")
            long_venue, short_venue = exchange.value, "MT4"
            exchange_funding = -settings.mt4_notional_usdt * funding_rate
            mt4_overnight = settings.mt4_notional_usdt / quote.bid * quote.overnight_short_usdt
        fee_rate = FEE_RATES.get(exchange, Decimal("0.0006"))
        fee = settings.mt4_notional_usdt * fee_rate * Decimal("2")
        basis_profit = settings.mt4_notional_usdt * spread_pct / Decimal("100")
        net = basis_profit + exchange_funding + mt4_overnight - fee
        reasons = []
        if spread_pct < settings.mt4_min_spread_pct:
            reasons.append(f"价差未达 {settings.mt4_min_spread_pct}%")
        if net < settings.mt4_min_net_profit_usdt:
            reasons.append(f"净利预估低于 {settings.mt4_min_net_profit_usdt}U")
        return Mt4SpreadOpportunity(
            instrument=quote.symbol,
            instrument_type=quote.instrument_type,
            mt4_symbol=quote.symbol,
            exchange=exchange,
            exchange_symbol=exchange_symbol,
            long_venue=long_venue,
            short_venue=short_venue,
            mt4_bid=q(quote.bid),
            mt4_ask=q(quote.ask),
            exchange_bid=q(exchange_bid),
            exchange_ask=q(exchange_ask),
            spread_pct=q(spread_pct),
            notional_usdt=q(settings.mt4_notional_usdt, "0.01"),
            margin_required_usdt=q(settings.mt4_notional_usdt / settings.mt4_default_leverage if settings.mt4_default_leverage > 0 else settings.mt4_notional_usdt, "0.01"),
            leverage=settings.mt4_default_leverage,
            estimated_exchange_funding_net=q(exchange_funding),
            estimated_mt4_overnight_net=q(mt4_overnight),
            estimated_open_close_fee=q(fee),
            estimated_net_profit=q(net),
            blocked_reasons=reasons,
            data_source=DataSource.LIVE,
            updated_at=datetime.now(timezone.utc),
        )

    def _blocked_missing_market(self, exchange: ExchangeName, quote: Mt4Quote, settings: BotSettings) -> Mt4SpreadOpportunity:
        return self._blocked(exchange, quote, "-", settings, "交易所未匹配到同品种合约")

    def _blocked(self, exchange: ExchangeName, quote: Mt4Quote, exchange_symbol: str, settings: BotSettings, reason: str) -> Mt4SpreadOpportunity:
        return Mt4SpreadOpportunity(
            instrument=quote.symbol,
            instrument_type=quote.instrument_type,
            mt4_symbol=quote.symbol,
            exchange=exchange,
            exchange_symbol=exchange_symbol,
            long_venue="-",
            short_venue="-",
            mt4_bid=q(quote.bid),
            mt4_ask=q(quote.ask),
            exchange_bid=Decimal("0"),
            exchange_ask=Decimal("0"),
            spread_pct=Decimal("0"),
            notional_usdt=q(settings.mt4_notional_usdt, "0.01"),
            margin_required_usdt=q(settings.mt4_notional_usdt / settings.mt4_default_leverage if settings.mt4_default_leverage > 0 else settings.mt4_notional_usdt, "0.01"),
            leverage=settings.mt4_default_leverage,
            estimated_exchange_funding_net=Decimal("0"),
            estimated_mt4_overnight_net=Decimal("0"),
            estimated_open_close_fee=Decimal("0"),
            estimated_net_profit=Decimal("0"),
            blocked_reasons=[reason],
            data_source=DataSource.LIVE,
            updated_at=datetime.now(timezone.utc),
        )

    def _exchange(self, exchange_name: ExchangeName):
        return build_ccxt_exchange(exchange_name, SWAP_EXCHANGE_IDS[exchange_name], "swap", timeout=12000)

    def _markets(self, exchange_name: ExchangeName, exchange) -> dict[str, str]:
        now = datetime.now(timezone.utc)
        cached = self._market_cache.get(exchange_name)
        if cached and (now - cached[0]).total_seconds() < 600:
            return cached[1]
        raw = exchange.load_markets()
        markets = {}
        for market in raw.values():
            if not market.get("swap") or market.get("quote") != "USDT" or market.get("active") is False:
                continue
            markets[normalized_market_symbol(market)] = market["symbol"]
        self._market_cache[exchange_name] = (now, markets)
        return markets

    def _funding(self, exchange_name: ExchangeName, exchange) -> dict[str, Decimal]:
        now = datetime.now(timezone.utc)
        cached = self._funding_cache.get(exchange_name)
        if cached and (now - cached[0]).total_seconds() < 60:
            return cached[1]
        if not exchange.has.get("fetchFundingRates"):
            return {}
        try:
            raw = exchange.fetch_funding_rates()
        except Exception:
            return {}
        rates = {}
        for item in raw.values() if isinstance(raw, dict) else raw:
            symbol = item.get("symbol")
            if symbol:
                rates[normalize_ccxt_symbol(symbol)] = decimal_from(item.get("fundingRate") or item.get("nextFundingRate"))
        self._funding_cache[exchange_name] = (now, rates)
        return rates

    def _mapped_symbol(self, mt4_symbol: str, markets: dict[str, str]) -> str | None:
        for alias in instrument_info(mt4_symbol)["aliases"]:
            if alias in markets:
                return alias
        return mt4_symbol if mt4_symbol in markets else None


def normalize_mt4_symbol(symbol: str) -> str:
    return "".join(ch for ch in symbol.upper() if ch.isalnum())


def instrument_info(symbol: str, fallback_type: str | None = None) -> dict[str, Any]:
    normalized = normalize_mt4_symbol(symbol)
    info = _configured_instruments().get(normalized) or DEFAULT_INSTRUMENTS.get(normalized)
    if info:
        return info
    return {"type": fallback_type or "commodity", "aliases": [normalized, f"{normalized}USDT"]}


def _configured_instruments() -> dict[str, dict[str, Any]]:
    if not SYMBOL_CONFIG_PATH.exists():
        return {}
    try:
        raw = json.loads(SYMBOL_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    result = {}
    for symbol, item in (raw.items() if isinstance(raw, dict) else []):
        if not isinstance(item, dict):
            continue
        normalized = normalize_mt4_symbol(symbol)
        aliases = [normalize_mt4_symbol(str(value)) for value in item.get("aliases", []) if value]
        result[normalized] = {
            "type": item.get("type") if item.get("type") in {"stock", "commodity"} else "commodity",
            "aliases": aliases or [normalized, f"{normalized}USDT"],
        }
    return result


def mt4_token_ok(payload_token: str | None, header_token: str | None) -> bool:
    expected = CredentialStore().mt4_token()
    if not expected:
        return True
    return (header_token or payload_token or "") == expected
