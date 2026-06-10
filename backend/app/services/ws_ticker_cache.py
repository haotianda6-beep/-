import asyncio
import threading
from datetime import datetime, timezone
from typing import Any, Literal

import ccxt.pro as ccxtpro

from app.core.models import ExchangeName
from app.services.exchange_factory import apply_modes_from_credentials
from app.services.live_market_types import SPOT_EXCHANGE_IDS, SWAP_EXCHANGE_IDS
from app.services.market_format import normalize_ccxt_symbol


MarketType = Literal["spot", "swap"]


class WSTickerCache:
    def __init__(self, max_symbols_per_stream: int = 3) -> None:
        self._lock = threading.RLock()
        self._subscriptions: dict[tuple[ExchangeName, MarketType], dict[str, str]] = {}
        self._tickers: dict[tuple[ExchangeName, MarketType, str], tuple[datetime, dict[str, Any]]] = {}
        self._started: set[tuple[ExchangeName, MarketType]] = set()
        self._issues: dict[tuple[ExchangeName, MarketType], str] = {}
        self.max_symbols_per_stream = max_symbols_per_stream

    def subscribe(self, exchange: ExchangeName, market_type: MarketType, symbol: str, ccxt_symbol: str) -> None:
        key = (exchange, market_type)
        with self._lock:
            subscriptions = self._subscriptions.setdefault(key, {})
            if symbol not in subscriptions and len(subscriptions) >= self.max_symbols_per_stream:
                return
            subscriptions[symbol] = ccxt_symbol
            if key in self._started:
                return
            self._started.add(key)
        thread = threading.Thread(target=self._run_thread, args=key, daemon=True, name=f"ws-{exchange}-{market_type}")
        thread.start()

    def get(self, exchange: ExchangeName, market_type: MarketType, symbol: str, max_age_seconds: float = 10) -> dict[str, Any] | None:
        with self._lock:
            item = self._tickers.get((exchange, market_type, symbol))
        if not item:
            return None
        updated_at, ticker = item
        if (datetime.now(timezone.utc) - updated_at).total_seconds() > max_age_seconds:
            return None
        return ticker

    def issues(self) -> list[str]:
        with self._lock:
            return [issue for issue in self._issues.values() if issue]

    def _run_thread(self, exchange: ExchangeName, market_type: MarketType) -> None:
        asyncio.run(self._run_exchange(exchange, market_type))

    async def _run_exchange(self, exchange_name: ExchangeName, market_type: MarketType) -> None:
        exchange = self._build_exchange(exchange_name, market_type)
        cursor = 0
        try:
            while True:
                with self._lock:
                    subscriptions = dict(self._subscriptions.get((exchange_name, market_type), {}))
                if not subscriptions:
                    await asyncio.sleep(1)
                    continue
                if exchange.has.get("watchTickers") and await self._watch_tickers(exchange, exchange_name, market_type, subscriptions):
                    continue
                items = list(subscriptions.items())
                symbol, ccxt_symbol = items[cursor % len(items)]
                cursor += 1
                await self._watch_one(exchange, exchange_name, market_type, symbol, ccxt_symbol)
        finally:
            await exchange.close()

    async def _watch_tickers(self, exchange, exchange_name: ExchangeName, market_type: MarketType, subscriptions: dict[str, str]) -> bool:
        params = self._watch_params(exchange_name, market_type)
        symbols = list(subscriptions.values())
        reverse = self._reverse_symbol_map(subscriptions)
        try:
            raw = await exchange.watch_tickers(symbols, params)
            for key, ticker in raw.items():
                symbol = reverse.get(key) or reverse.get(normalize_ccxt_symbol(key)) or reverse.get(ticker.get("symbol"))
                if symbol:
                    self._store_ticker(exchange_name, market_type, symbol, ticker)
            self._set_issue(exchange_name, market_type, "")
            return True
        except Exception as exc:  # noqa: BLE001
            self._set_issue(exchange_name, market_type, f"{exchange_name} {market_type} WS tickers 异常: {str(exc)[:180]}")
            await asyncio.sleep(2)
            return False

    async def _watch_one(self, exchange, exchange_name: ExchangeName, market_type: MarketType, symbol: str, ccxt_symbol: str) -> None:
        try:
            ticker = await exchange.watch_ticker(ccxt_symbol, self._watch_params(exchange_name, market_type))
            self._store_ticker(exchange_name, market_type, symbol, ticker)
            self._set_issue(exchange_name, market_type, "")
        except Exception as exc:  # noqa: BLE001
            self._set_issue(exchange_name, market_type, f"{exchange_name} {market_type} WS ticker 异常: {str(exc)[:180]}")
            await asyncio.sleep(2)

    def _reverse_symbol_map(self, subscriptions: dict[str, str]) -> dict[str, str]:
        result = {}
        for symbol, ccxt_symbol in subscriptions.items():
            result[ccxt_symbol] = symbol
            result[normalize_ccxt_symbol(ccxt_symbol)] = symbol
        return result

    def _store_ticker(self, exchange: ExchangeName, market_type: MarketType, symbol: str, ticker: dict[str, Any]) -> None:
        key = (exchange, market_type, symbol)
        with self._lock:
            previous = self._tickers.get(key, (None, {}))[1]
            normalized = dict(ticker)
            for field in ("bid", "ask", "last", "close", "quoteVolume", "baseVolume"):
                if normalized.get(field) is None and previous.get(field) is not None:
                    normalized[field] = previous[field]
            self._tickers[key] = (datetime.now(timezone.utc), normalized)

    def _set_issue(self, exchange: ExchangeName, market_type: MarketType, issue: str) -> None:
        with self._lock:
            self._issues[(exchange, market_type)] = issue

    def _build_exchange(self, exchange_name: ExchangeName, market_type: MarketType):
        exchange_id = SPOT_EXCHANGE_IDS[exchange_name] if market_type == "spot" else SWAP_EXCHANGE_IDS[exchange_name]
        exchange = getattr(ccxtpro, exchange_id)({
            "enableRateLimit": True,
            "timeout": 20000,
            "options": {"defaultType": market_type},
        })
        apply_modes_from_credentials(exchange, exchange_name)
        return exchange

    def _watch_params(self, exchange_name: ExchangeName, market_type: MarketType) -> dict[str, Any]:
        if exchange_name == ExchangeName.GATE and market_type == "swap":
            return {"settle": "usdt"}
        if market_type == "swap":
            return {"type": "swap"}
        return {}
