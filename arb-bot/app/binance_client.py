from __future__ import annotations

import asyncio
import hmac
import hashlib
import json
import logging
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import httpx
import websockets

from app.config import Settings
from app.logger import masked
from app.models import AccountSnapshot, BinanceFundingInfo, ExchangeFilters, MarketQuote, OrderRequest, OrderStatus, OrderUpdate, Side, utc_now_ms


logger = logging.getLogger(__name__)


class BinanceError(Exception):
    pass


class BinanceBaseClient:
    maker_fee_rate: Decimal | None = None
    filters: ExchangeFilters

    async def start(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    def latest_quote(self) -> MarketQuote | None:
        raise NotImplementedError

    def latest_funding(self) -> BinanceFundingInfo | None:
        raise NotImplementedError

    async def place_post_only_order(self, request: OrderRequest) -> OrderUpdate:
        raise NotImplementedError

    async def place_market_order(self, request: OrderRequest) -> OrderUpdate:
        raise NotImplementedError

    async def cancel_order(self, order_id: str) -> OrderUpdate | None:
        raise NotImplementedError

    async def get_order(self, order_id: str) -> OrderUpdate | None:
        raise NotImplementedError

    async def open_orders(self) -> list[OrderUpdate]:
        raise NotImplementedError

    async def position_quantity(self) -> Decimal:
        raise NotImplementedError

    async def account_snapshot(self) -> AccountSnapshot | None:
        raise NotImplementedError


class PaperBinanceClient(BinanceBaseClient):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.filters = ExchangeFilters(
            tick_size=settings.binance_tick_size,
            qty_step=settings.binance_qty_step,
            min_qty=settings.binance_min_qty,
        )
        self.maker_fee_rate = settings.binance_maker_fee_rate or Decimal("0.0002")
        self._quote: MarketQuote | None = None
        self._orders: dict[str, OrderUpdate] = {}
        self._created_ms: dict[str, int] = {}
        self._client = httpx.AsyncClient(base_url=settings.binance_base_url, timeout=10, trust_env=False)
        self._tasks: list[asyncio.Task] = []
        self._use_live_market_data = bool(settings.binance_api_key and settings.binance_api_secret)
        self._funding: BinanceFundingInfo | None = None
        self._account: AccountSnapshot | None = None
        self._account_cache_ms = 0

    async def start(self) -> None:
        if self._use_live_market_data:
            await self._load_live_metadata()
            self._tasks.append(asyncio.create_task(self._book_ticker_loop()))
            await self._refresh_funding_info()
            self._tasks.append(asyncio.create_task(self._funding_loop()))
        logger.info(
            "Binance paper client ready symbol=%s maker_fee=%s source=%s",
            self.settings.binance_symbol,
            self.maker_fee_rate,
            "live-api" if self._use_live_market_data else ("env" if self.settings.binance_maker_fee_rate is not None else "paper-default"),
        )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await self._client.aclose()
        return None

    def set_quote(self, bid: Decimal, ask: Decimal) -> None:
        self._quote = MarketQuote(symbol=self.settings.binance_symbol, bid=bid, ask=ask)

    def latest_quote(self) -> MarketQuote | None:
        return self._quote

    def latest_funding(self) -> BinanceFundingInfo | None:
        return self._funding

    def clear_orders(self) -> None:
        self._orders.clear()
        self._created_ms.clear()

    async def place_post_only_order(self, request: OrderRequest) -> OrderUpdate:
        if request.price is None:
            raise BinanceError("post only order requires price")
        reject = self._post_only_would_take(request.side, request.price)
        order = OrderUpdate(
            order_id=f"paper_{uuid4().hex[:16]}",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            status=OrderStatus.REJECTED if reject else OrderStatus.NEW,
            price=request.price,
            orig_qty=request.quantity,
            reduce_only=request.reduce_only,
            message="post only would take liquidity" if reject else None,
        )
        self._orders[order.order_id] = order
        self._created_ms[order.order_id] = utc_now_ms()
        return order

    async def place_market_order(self, request: OrderRequest) -> OrderUpdate:
        quote = self.latest_quote()
        if not quote:
            raise BinanceError("paper quote missing")
        fill = quote.ask if request.side == Side.BUY else quote.bid
        order = OrderUpdate(
            order_id=f"paper_mkt_{uuid4().hex[:16]}",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            status=OrderStatus.FILLED,
            price=fill,
            orig_qty=request.quantity,
            executed_qty=request.quantity,
            avg_price=fill,
            is_maker=False,
            reduce_only=request.reduce_only,
        )
        self._orders[order.order_id] = order
        return order

    async def cancel_order(self, order_id: str) -> OrderUpdate | None:
        order = self._orders.get(order_id)
        if not order:
            return None
        if order.status in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}:
            order = order.model_copy(update={"status": OrderStatus.CANCELED})
            self._orders[order_id] = order
        return order

    async def get_order(self, order_id: str) -> OrderUpdate | None:
        await self._auto_fill(order_id)
        return self._orders.get(order_id)

    async def open_orders(self) -> list[OrderUpdate]:
        return [
            order
            for order in self._orders.values()
            if order.status in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}
        ]

    async def position_quantity(self) -> Decimal:
        return Decimal("0")

    async def account_snapshot(self) -> AccountSnapshot | None:
        if not self._use_live_market_data:
            return self._account
        now = utc_now_ms()
        if self._account and now - self._account_cache_ms <= 3000:
            return self._account
        try:
            self._account = _parse_account_snapshot(await self._signed("GET", "/fapi/v2/account", {}))
            self._account_cache_ms = now
        except Exception as exc:  # noqa: BLE001
            logger.warning("Binance paper account snapshot unavailable: %s", str(exc)[:160])
        return self._account

    async def simulate_fill(self, order_id: str, quantity: Decimal, price: Decimal | None = None) -> OrderUpdate:
        order = self._orders[order_id]
        executed = min(order.orig_qty, order.executed_qty + quantity)
        status = OrderStatus.FILLED if executed >= order.orig_qty else OrderStatus.PARTIALLY_FILLED
        avg = price or order.price
        updated = order.model_copy(update={"executed_qty": executed, "avg_price": avg, "status": status})
        self._orders[order_id] = updated
        return updated

    def _post_only_would_take(self, side: Side, price: Decimal) -> bool:
        quote = self.latest_quote()
        if not quote:
            return False
        if side == Side.BUY:
            return price >= quote.ask
        return price <= quote.bid

    async def _auto_fill(self, order_id: str) -> None:
        if not self.settings.paper_auto_fill:
            return
        order = self._orders.get(order_id)
        if not order or order.status != OrderStatus.NEW:
            return
        quote = self.latest_quote()
        if not quote:
            return
        touched = quote.ask <= order.price if order.side == Side.BUY else quote.bid >= order.price
        if not touched:
            return
        age = utc_now_ms() - self._created_ms.get(order_id, utc_now_ms())
        if age < self.settings.paper_fill_delay_ms:
            return
        await self.simulate_fill(order_id, order.orig_qty, order.price)

    async def _load_live_metadata(self) -> None:
        try:
            data = await self._public("GET", "/fapi/v1/exchangeInfo")
            symbol = next(item for item in data["symbols"] if item["symbol"] == self.settings.binance_symbol)
            filters = {item["filterType"]: item for item in symbol["filters"]}
            self.filters = ExchangeFilters(
                tick_size=Decimal(filters["PRICE_FILTER"]["tickSize"]),
                qty_step=Decimal(filters["LOT_SIZE"]["stepSize"]),
                min_qty=Decimal(filters["LOT_SIZE"]["minQty"]),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Binance paper metadata unavailable: %s", str(exc)[:160])
        try:
            data = await self._signed("GET", "/fapi/v1/commissionRate", {"symbol": self.settings.binance_symbol})
            self.maker_fee_rate = Decimal(str(data["makerCommissionRate"]))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Binance paper commission unavailable; using configured maker fee: %s", str(exc)[:160])

    async def _book_ticker_loop(self) -> None:
        stream = f"{self.settings.binance_symbol.lower()}@bookTicker"
        url = f"{self.settings.binance_ws_url}/ws/{stream}"
        while True:
            try:
                async with websockets.connect(url, ping_interval=15, ping_timeout=10) as ws:
                    async for message in ws:
                        data = json.loads(message)
                        self._quote = MarketQuote(
                            symbol=self.settings.binance_symbol,
                            bid=Decimal(str(data["b"])),
                            ask=Decimal(str(data["a"])),
                            timestamp_ms=int(data.get("E") or utc_now_ms()),
                        )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Binance paper bookTicker reconnecting: %s", str(exc)[:160])
                await asyncio.sleep(2)

    async def _funding_loop(self) -> None:
        while True:
            try:
                await self._refresh_funding_info()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Binance funding info unavailable: %s", str(exc)[:160])
            await asyncio.sleep(30)

    async def _refresh_funding_info(self) -> None:
        data = await self._public("GET", "/fapi/v1/premiumIndex", {"symbol": self.settings.binance_symbol})
        self._funding = _parse_funding_info(data, self.settings.binance_symbol)

    async def _public(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._client.request(method, path, params=params)
        response.raise_for_status()
        return response.json()

    async def _signed(self, method: str, path: str, params: dict[str, Any]) -> Any:
        key = self.settings.binance_api_key.get_secret_value() if self.settings.binance_api_key else ""
        secret = self.settings.binance_api_secret.get_secret_value() if self.settings.binance_api_secret else ""
        if not key or not secret:
            raise BinanceError("Binance API credentials missing")
        payload = {**params, "timestamp": int(time.time() * 1000), "recvWindow": 5000}
        query = urlencode(payload)
        signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        response = await self._client.request(
            method,
            path,
            params={**payload, "signature": signature},
            headers={"X-MBX-APIKEY": key},
        )
        if response.status_code >= 400:
            raise BinanceError(response.text[:240])
        return response.json()


class BinanceFuturesClient(BinanceBaseClient):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.filters = ExchangeFilters(tick_size=settings.binance_tick_size, qty_step=settings.binance_qty_step)
        self._client = httpx.AsyncClient(base_url=settings.binance_base_url, timeout=10, trust_env=False)
        self._quote: MarketQuote | None = None
        self._funding: BinanceFundingInfo | None = None
        self._listen_key: str | None = None
        self._tasks: list[asyncio.Task] = []
        self._hedge_mode = False
        self._account: AccountSnapshot | None = None
        self._account_cache_ms = 0

    async def start(self) -> None:
        await self._load_exchange_info()
        await self._load_commission_rate()
        await self._configure_leverage()
        await self._detect_position_mode()
        await self._refresh_funding_info()
        self._tasks.append(asyncio.create_task(self._book_ticker_loop()))
        self._tasks.append(asyncio.create_task(self._funding_loop()))
        logger.info(
            "Binance live client ready symbol=%s key=%s maker_fee=%s hedge_mode=%s",
            self.settings.binance_symbol,
            masked(self.settings.binance_api_key.get_secret_value() if self.settings.binance_api_key else None),
            self.maker_fee_rate,
            self._hedge_mode,
        )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await self._client.aclose()

    def latest_quote(self) -> MarketQuote | None:
        return self._quote

    def latest_funding(self) -> BinanceFundingInfo | None:
        return self._funding

    async def place_post_only_order(self, request: OrderRequest) -> OrderUpdate:
        params = self._order_params(request, order_type="LIMIT")
        params["timeInForce"] = "GTX"
        raw = await self._signed("POST", "/fapi/v1/order", params)
        return self._parse_order(raw, request)

    async def place_market_order(self, request: OrderRequest) -> OrderUpdate:
        raw = await self._signed("POST", "/fapi/v1/order", self._order_params(request, order_type="MARKET"))
        return self._parse_order(raw, request)

    async def cancel_order(self, order_id: str) -> OrderUpdate | None:
        raw = await self._signed("DELETE", "/fapi/v1/order", {"symbol": self.settings.binance_symbol, "orderId": order_id})
        return self._parse_order(raw)

    async def get_order(self, order_id: str) -> OrderUpdate | None:
        raw = await self._signed("GET", "/fapi/v1/order", {"symbol": self.settings.binance_symbol, "orderId": order_id})
        return self._parse_order(raw)

    async def open_orders(self) -> list[OrderUpdate]:
        raw = await self._signed("GET", "/fapi/v1/openOrders", {"symbol": self.settings.binance_symbol})
        return [self._parse_order(item) for item in raw]

    async def position_quantity(self) -> Decimal:
        raw = await self._signed("GET", "/fapi/v2/positionRisk", {"symbol": self.settings.binance_symbol})
        item = raw[0] if isinstance(raw, list) and raw else raw
        return Decimal(str(item.get("positionAmt") or "0"))

    async def account_snapshot(self) -> AccountSnapshot | None:
        now = utc_now_ms()
        if self._account and now - self._account_cache_ms <= 3000:
            return self._account
        try:
            self._account = _parse_account_snapshot(await self._signed("GET", "/fapi/v2/account", {}))
            self._account_cache_ms = now
        except Exception as exc:  # noqa: BLE001
            logger.warning("Binance account snapshot unavailable: %s", str(exc)[:160])
        return self._account

    async def _load_exchange_info(self) -> None:
        data = await self._public("GET", "/fapi/v1/exchangeInfo")
        symbol = next(item for item in data["symbols"] if item["symbol"] == self.settings.binance_symbol)
        filters = {item["filterType"]: item for item in symbol["filters"]}
        self.filters = ExchangeFilters(
            tick_size=Decimal(filters["PRICE_FILTER"]["tickSize"]),
            qty_step=Decimal(filters["LOT_SIZE"]["stepSize"]),
            min_qty=Decimal(filters["LOT_SIZE"]["minQty"]),
        )

    async def _load_commission_rate(self) -> None:
        try:
            data = await self._signed("GET", "/fapi/v1/commissionRate", {"symbol": self.settings.binance_symbol})
            self.maker_fee_rate = Decimal(str(data["makerCommissionRate"]))
        except Exception:
            if self.settings.binance_maker_fee_rate is None:
                raise
            self.maker_fee_rate = self.settings.binance_maker_fee_rate
            logger.warning("Binance commissionRate unavailable; using env maker fee")

    async def _configure_leverage(self) -> None:
        await self._signed(
            "POST",
            "/fapi/v1/leverage",
            {"symbol": self.settings.binance_symbol, "leverage": self.settings.binance_leverage},
        )
        logger.info("Binance leverage configured symbol=%s leverage=%sx", self.settings.binance_symbol, self.settings.binance_leverage)

    async def _detect_position_mode(self) -> None:
        data = await self._signed("GET", "/fapi/v1/positionSide/dual", {})
        self._hedge_mode = bool(data.get("dualSidePosition"))

    async def _book_ticker_loop(self) -> None:
        stream = f"{self.settings.binance_symbol.lower()}@bookTicker"
        url = f"{self.settings.binance_ws_url}/ws/{stream}"
        while True:
            try:
                async with websockets.connect(url, ping_interval=15, ping_timeout=10) as ws:
                    async for message in ws:
                        data = json.loads(message)
                        self._quote = MarketQuote(
                            symbol=self.settings.binance_symbol,
                            bid=Decimal(str(data["b"])),
                            ask=Decimal(str(data["a"])),
                            timestamp_ms=int(data.get("E") or utc_now_ms()),
                        )
            except Exception as exc:
                logger.warning("Binance bookTicker reconnecting: %s", str(exc)[:160])
                await asyncio.sleep(2)

    async def _funding_loop(self) -> None:
        while True:
            try:
                await self._refresh_funding_info()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Binance funding info unavailable: %s", str(exc)[:160])
            await asyncio.sleep(30)

    async def _refresh_funding_info(self) -> None:
        data = await self._public("GET", "/fapi/v1/premiumIndex", {"symbol": self.settings.binance_symbol})
        self._funding = _parse_funding_info(data, self.settings.binance_symbol)

    async def _public(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._client.request(method, path, params=params)
        response.raise_for_status()
        return response.json()

    async def _signed(self, method: str, path: str, params: dict[str, Any]) -> Any:
        key = self.settings.binance_api_key.get_secret_value() if self.settings.binance_api_key else ""
        secret = self.settings.binance_api_secret.get_secret_value() if self.settings.binance_api_secret else ""
        if not key or not secret:
            raise BinanceError("Binance API credentials missing")
        payload = {**params, "timestamp": int(time.time() * 1000), "recvWindow": 5000}
        query = urlencode(payload)
        signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        response = await self._client.request(
            method,
            path,
            params={**payload, "signature": signature},
            headers={"X-MBX-APIKEY": key},
        )
        if response.status_code >= 400:
            raise BinanceError(response.text[:240])
        return response.json()

    def _order_params(self, request: OrderRequest, order_type: str) -> dict[str, Any]:
        params: dict[str, Any] = {
            "symbol": request.symbol,
            "side": request.side.value,
            "type": order_type,
            "quantity": str(request.quantity),
            "newClientOrderId": request.client_order_id,
        }
        if request.price is not None and order_type == "LIMIT":
            params["price"] = str(request.price)
        if request.reduce_only:
            if self._hedge_mode:
                params["positionSide"] = request.position_side or ("SHORT" if request.side == Side.BUY else "LONG")
            else:
                params["reduceOnly"] = "true"
        return params

    def _parse_order(self, raw: dict[str, Any], request: OrderRequest | None = None) -> OrderUpdate:
        price = Decimal(str(raw.get("price") or (request.price if request and request.price else "0")))
        avg = Decimal(str(raw.get("avgPrice") or raw.get("average") or "0"))
        return OrderUpdate(
            order_id=str(raw.get("orderId") or raw.get("order_id")),
            client_order_id=str(raw.get("clientOrderId") or (request.client_order_id if request else "")),
            symbol=str(raw.get("symbol") or self.settings.binance_symbol),
            side=Side(str(raw.get("side") or (request.side.value if request else "BUY"))),
            status=OrderStatus(str(raw.get("status") or "NEW")),
            price=price,
            orig_qty=Decimal(str(raw.get("origQty") or (request.quantity if request else "0"))),
            executed_qty=Decimal(str(raw.get("executedQty") or "0")),
            avg_price=avg if avg > 0 else price,
            reduce_only=bool(request.reduce_only) if request else str(raw.get("reduceOnly")).lower() == "true",
        )


def _parse_funding_info(data: dict[str, Any], symbol: str) -> BinanceFundingInfo:
    return BinanceFundingInfo(
        symbol=str(data.get("symbol") or symbol),
        funding_rate=Decimal(str(data.get("lastFundingRate") or "0")),
        next_funding_time_ms=int(data.get("nextFundingTime") or 0),
        mark_price=Decimal(str(data["markPrice"])) if data.get("markPrice") is not None else None,
    )


def _parse_account_snapshot(data: dict[str, Any]) -> AccountSnapshot:
    return AccountSnapshot(
        venue="币安合约",
        balance=_optional_decimal(data.get("totalWalletBalance")),
        equity=_optional_decimal(data.get("totalMarginBalance")),
        available=_optional_decimal(data.get("availableBalance")),
        used_margin=_optional_decimal(data.get("totalInitialMargin")),
        unrealized_pnl=_optional_decimal(data.get("totalUnrealizedProfit")),
        currency="USDT",
    )


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))
