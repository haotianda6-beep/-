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
    "BA": {"type": "stock", "aliases": ["BAUSDT"]},
    "BABA": {"type": "stock", "aliases": ["BABAUSDT"]},
    "BIDU": {"type": "stock", "aliases": ["BIDUUSDT"]},
    "C": {"type": "stock", "aliases": ["CUSDT"]},
    "GILD": {"type": "stock", "aliases": ["GILDUSDT"]},
    "GOOG": {"type": "stock", "aliases": ["GOOGUSDT", "GOOGLUSDT"]},
    "GOOGL": {"type": "stock", "aliases": ["GOOGLUSDT", "GOOGUSDT"]},
    "IBM": {"type": "stock", "aliases": ["IBMUSDT"]},
    "JD": {"type": "stock", "aliases": ["JDUSDT"]},
    "KO": {"type": "stock", "aliases": ["KOUSDT"]},
    "MCD": {"type": "stock", "aliases": ["MCDUSDT"]},
    "META": {"type": "stock", "aliases": ["METAUSDT"]},
    "MSFT": {"type": "stock", "aliases": ["MSFTUSDT"]},
    "NFLX": {"type": "stock", "aliases": ["NFLXUSDT"]},
    "NKE": {"type": "stock", "aliases": ["NKEUSDT"]},
    "NTES": {"type": "stock", "aliases": ["NTESUSDT"]},
    "NVDA": {"type": "stock", "aliases": ["NVDAUSDT"]},
    "SBUX": {"type": "stock", "aliases": ["SBUXUSDT"]},
    "TSLA": {"type": "stock", "aliases": ["TSLAUSDT"]},
    "V": {"type": "stock", "aliases": ["VUSDT"]},
}
MT4_CONTRACT_SIZE_OVERRIDES: dict[str, Decimal] = {
    "XAUUSD": Decimal("100"),
    "XAGUSD": Decimal("1000"),
}
MT4_MIN_LOTS = Decimal("0.01")


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
        raw_symbol = normalize_mt4_symbol(payload.symbol)
        instrument = instrument_info(raw_symbol, payload.instrument_type)
        symbol = str(instrument.get("symbol") or raw_symbol)
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
            contract_size=_contract_size(symbol, payload.contract_size),
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
        self._okx_market_objects: tuple[datetime, list[dict[str, Any]]] | None = None

    def clear_caches(self) -> None:
        self._market_cache = {}
        self._funding_cache = {}
        self._okx_market_objects = None

    def scan(self, settings: BotSettings) -> tuple[list[Mt4SpreadOpportunity], list[Mt4SpreadOpportunity], list[str]]:
        if not settings.mt4_spread_enabled:
            return [], [], []
        issues: list[str] = []
        quotes = self.quote_store.quotes(settings.mt4_max_quote_age_seconds)
        stale = self.quote_store.all_quotes()
        if not quotes and stale:
            issues.append("MT4 报价已过期，等待插件继续推送。")
        rows: list[Mt4SpreadOpportunity] = []
        for exchange in ExchangeName:
            rows.extend(self._exchange_rows_for_quotes(exchange, quotes, settings, issues))
        opportunities = [item for item in rows if not item.blocked_reasons]
        candidates = sorted(rows, key=lambda item: (len(item.blocked_reasons), -item.estimated_net_profit))[:50]
        return sorted(opportunities, key=lambda item: item.estimated_net_profit, reverse=True), candidates, issues

    def _exchange_rows(self, exchange: ExchangeName, quote: Mt4Quote, settings: BotSettings, issues: list[str]) -> list[Mt4SpreadOpportunity]:
        return self._exchange_rows_for_quotes(exchange, [quote], settings, issues)

    def _exchange_rows_for_quotes(self, exchange: ExchangeName, quotes: list[Mt4Quote], settings: BotSettings, issues: list[str]) -> list[Mt4SpreadOpportunity]:
        if not quotes:
            return []
        try:
            instance = self._exchange(exchange)
            markets = self._markets(exchange, instance)
            funding = self._funding(exchange, instance)
        except Exception as exc:  # noqa: BLE001
            issues.append(f"{exchange}: MT4 对比合约行情读取失败 {str(exc)[:180]}")
            return []

        rows: list[Mt4SpreadOpportunity] = []
        for quote in quotes:
            symbol = self._mapped_symbol(quote.symbol, markets)
            if not symbol:
                continue
            try:
                ticker = instance.fetch_ticker(markets[symbol])
            except Exception as exc:  # noqa: BLE001
                issues.append(f"{exchange} {quote.symbol}: MT4 对比合约行情读取失败 {str(exc)[:160]}")
                continue
            rows.append(self._row(exchange, quote, symbol, ticker, funding.get(symbol, Decimal("0")), settings))
        return rows

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
            hedge_base_quantity = self._effective_hedge_base(quote, settings.mt4_notional_usdt / quote.ask)
            mt4_lots = self._mt4_lots(quote, hedge_base_quantity)
            effective_notional = hedge_base_quantity * quote.ask
            exchange_funding = effective_notional * funding_rate
            mt4_overnight = self._mt4_overnight(quote, mt4_lots, quote.overnight_long_usdt)
        else:
            spread_pct = (quote.bid - exchange_ask) / exchange_ask * Decimal("100")
            long_venue, short_venue = exchange.value, "MT4"
            hedge_base_quantity = self._effective_hedge_base(quote, settings.mt4_notional_usdt / quote.bid)
            mt4_lots = self._mt4_lots(quote, hedge_base_quantity)
            effective_notional = hedge_base_quantity * quote.bid
            exchange_funding = -effective_notional * funding_rate
            mt4_overnight = self._mt4_overnight(quote, mt4_lots, quote.overnight_short_usdt)
        fee_rate = FEE_RATES.get(exchange, Decimal("0.0006"))
        fee = effective_notional * fee_rate * Decimal("2")
        basis_profit = effective_notional * spread_pct / Decimal("100")
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
            notional_usdt=q(effective_notional, "0.01"),
            margin_required_usdt=q(effective_notional / settings.mt4_default_leverage if settings.mt4_default_leverage > 0 else effective_notional, "0.01"),
            leverage=settings.mt4_default_leverage,
            mt4_contract_size=q(quote.contract_size, "0.000001"),
            mt4_lots=q(mt4_lots, "0.000001"),
            hedge_base_quantity=q(hedge_base_quantity, "0.000001"),
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
            mt4_contract_size=q(quote.contract_size, "0.000001"),
            mt4_lots=Decimal("0"),
            hedge_base_quantity=Decimal("0"),
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
            if exchange_name == ExchangeName.OKX:
                self._load_markets(exchange_name, exchange)
            return cached[1]
        raw = self._load_markets(exchange_name, exchange)
        markets = {}
        for market in raw.values():
            if not market.get("swap") or market.get("quote") != "USDT" or market.get("active") is False:
                continue
            markets[normalized_market_symbol(market)] = market["symbol"]
        self._market_cache[exchange_name] = (now, markets)
        return markets

    def _load_markets(self, exchange_name: ExchangeName, exchange) -> dict[str, Any]:
        if exchange_name != ExchangeName.OKX:
            return exchange.load_markets()
        now = datetime.now(timezone.utc)
        cached = self._okx_market_objects
        if cached and (now - cached[0]).total_seconds() < 600:
            exchange.set_markets(cached[1])
            return exchange.markets
        raw = exchange.fetch_markets({"instType": "SWAP"})
        cleaned = [market for market in raw if market.get("id") and market.get("symbol")]
        exchange.set_markets(cleaned)
        self._okx_market_objects = (now, cleaned)
        return exchange.markets

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

    def _mt4_lots(self, quote: Mt4Quote, hedge_base_quantity: Decimal) -> Decimal:
        return hedge_base_quantity / quote.contract_size if quote.contract_size > 0 else hedge_base_quantity

    def _effective_hedge_base(self, quote: Mt4Quote, requested_base_quantity: Decimal) -> Decimal:
        min_base_quantity = quote.contract_size * MT4_MIN_LOTS
        return max(requested_base_quantity, min_base_quantity)

    def _mt4_overnight(self, quote: Mt4Quote, mt4_lots: Decimal, overnight_per_payload_lots: Decimal) -> Decimal:
        payload_lots = quote.lots if quote.lots > 0 else Decimal("1")
        return mt4_lots / payload_lots * overnight_per_payload_lots


def normalize_mt4_symbol(symbol: str) -> str:
    return "".join(ch for ch in symbol.upper() if ch.isalnum())


def _contract_size(symbol: str, payload_contract_size: Decimal) -> Decimal:
    override = MT4_CONTRACT_SIZE_OVERRIDES.get(symbol)
    if override:
        return override
    return payload_contract_size if payload_contract_size > 0 else Decimal("1")


def instrument_info(symbol: str, fallback_type: str | None = None) -> dict[str, Any]:
    normalized = normalize_mt4_symbol(symbol)
    info = _configured_instruments().get(normalized) or _default_instrument(normalized)
    if info:
        return info
    return {"symbol": normalized, "type": fallback_type or "commodity", "aliases": [normalized, f"{normalized}USDT"]}


def _default_instrument(normalized: str) -> dict[str, Any] | None:
    direct = DEFAULT_INSTRUMENTS.get(normalized)
    if direct:
        return {"symbol": normalized, **direct}
    for base in sorted(DEFAULT_INSTRUMENTS, key=len, reverse=True):
        if normalized.startswith(base) or normalized.endswith(base):
            return {"symbol": base, **DEFAULT_INSTRUMENTS[base]}
    return None


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
            "symbol": normalize_mt4_symbol(str(item.get("symbol") or normalized)),
            "type": item.get("type") if item.get("type") in {"stock", "commodity"} else "commodity",
            "aliases": aliases or [normalized, f"{normalized}USDT"],
        }
    return result


def mt4_token_ok(payload_token: str | None, header_token: str | None) -> bool:
    expected = CredentialStore().mt4_token()
    if not expected:
        return True
    return (header_token or payload_token or "") == expected
