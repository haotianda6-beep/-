import time
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.core.market_math import FEE_RATES, q
from app.core.models import BotSettings, CashCarryOpportunity, CashCarryPositionRow, ExchangeName, PositionSnapshot
from app.services.exchange_factory import build_ccxt_exchange
from app.services.live_market_types import SPOT_EXCHANGE_IDS, SWAP_EXCHANGE_IDS
from app.services.live_read import decimal_from
from app.services.ws_ticker_cache import WSTickerCache


class CashCarryPositionBuilder:
    def __init__(self, ticker_cache: WSTickerCache | None = None) -> None:
        self.ticker_cache = ticker_cache
        self._spot_balance_cache: dict[tuple[ExchangeName, str], tuple[float, Decimal]] = {}
        self._contract_size_cache: dict[tuple[ExchangeName, str], tuple[float, Decimal]] = {}
        self._exit_price_cache: dict[tuple[ExchangeName, str, str], tuple[float, Decimal]] = {}

    def clear_caches(self) -> None:
        self._spot_balance_cache = {}
        self._contract_size_cache = {}
        self._exit_price_cache = {}

    def build(self, positions: list[PositionSnapshot], prices: list[CashCarryOpportunity], settings: BotSettings) -> list[CashCarryPositionRow]:
        rows = []
        price_map = {(ExchangeName(item.exchange), item.symbol): item for item in prices}
        state_map = self._state_records()
        seen: set[tuple[ExchangeName, str]] = set()
        for position in positions:
            if position.side != "short":
                continue
            exchange = ExchangeName(position.exchange)
            price = price_map.get((exchange, position.symbol))
            seen.add((exchange, position.symbol))
            try:
                rows.append(self._row(exchange, position, price, state_map.get((exchange, position.symbol)), settings))
            except Exception:
                continue
        for key, state in state_map.items():
            if key in seen:
                continue
            exchange, symbol = key
            try:
                rows.append(self._state_only_row(exchange, symbol, price_map.get(key), state, settings))
            except Exception:
                continue
        return rows

    def has_open_state_records(self) -> bool:
        return bool(self._state_records())

    def _row(
        self,
        exchange: ExchangeName,
        position: PositionSnapshot,
        price: CashCarryOpportunity | None,
        state: dict[str, Any] | None,
        settings: BotSettings,
    ) -> CashCarryPositionRow:
        base = position.symbol.removesuffix("USDT")
        swap_symbol = f"{base}/USDT:USDT"
        spot_quantity = self._cached_spot_quantity(exchange, base)
        contract_size = self._cached_contract_size(exchange, swap_symbol)
        perp_contracts = Decimal(str(position.quantity))
        perp_base = perp_contracts * contract_size
        spot_price, perp_mark = self._close_prices(exchange, position.symbol, base, price, position.mark_price)
        spot_entry = self._entry_price(state, "spot_entry_price", spot_price)
        gap = spot_quantity - perp_base
        basis = ((perp_mark - spot_price) / spot_price * Decimal("100")) if spot_price > 0 else Decimal("0")
        spot_pnl = (spot_price - spot_entry) * spot_quantity
        perp_pnl = self._perp_unrealized(position.side, perp_base, position.entry_price, perp_mark, position.unrealized_pnl)
        funding_rate_pct = price.funding_rate_pct if price else Decimal("0")
        funding_income = self._funding_income(position.side, perp_base, perp_mark, funding_rate_pct)
        open_fee, close_fee = self._fees(exchange, spot_quantity, spot_entry, spot_price, perp_base, position.entry_price, perp_mark)
        net = spot_pnl + perp_pnl + funding_income - open_fee - close_fee
        return CashCarryPositionRow(
            exchange=exchange,
            symbol=position.symbol,
            status=self._status(spot_quantity, perp_base),
            spot_quantity=q(spot_quantity, "0.000001"),
            spot_entry_price=q(spot_entry),
            spot_price=q(spot_price),
            spot_unrealized_pnl=q(spot_pnl),
            perp_side=position.side,
            perp_contracts=perp_contracts,
            perp_base_quantity=q(perp_base, "0.000001"),
            contract_size=contract_size,
            perp_entry_price=position.entry_price,
            perp_mark_price=q(perp_mark),
            leverage=position.leverage,
            perp_unrealized_pnl=q(perp_pnl),
            estimated_funding_rate_pct=q(funding_rate_pct),
            estimated_funding_income=q(funding_income),
            estimated_open_fee=q(open_fee),
            estimated_close_fee=q(close_fee),
            current_net_profit=q(net),
            quantity_gap=q(gap, "0.000001"),
            basis_pct=q(basis),
            add_count=self._add_count(state),
            add_notional_usdt=self._add_notional(state, settings),
            next_add_trigger_basis_pct=self._next_add_trigger_basis(state, settings),
            updated_at=datetime.now(timezone.utc),
        )

    def _state_only_row(
        self,
        exchange: ExchangeName,
        symbol: str,
        price: CashCarryOpportunity | None,
        state: dict[str, Any],
        settings: BotSettings,
    ) -> CashCarryPositionRow:
        base = symbol.removesuffix("USDT")
        swap_symbol = f"{base}/USDT:USDT"
        spot_quantity = self._cached_spot_quantity(exchange, base)
        contract_size = self._cached_contract_size(exchange, swap_symbol)
        spot_price, perp_mark = self._close_prices(exchange, symbol, base, price, decimal_from(state.get("perp_entry_price")))
        spot_entry = self._entry_price(state, "spot_entry_price", spot_price)
        perp_entry = decimal_from(state.get("perp_entry_price"))
        spot_pnl = (spot_price - spot_entry) * spot_quantity
        perp_base = Decimal("0")
        funding_rate_pct = price.funding_rate_pct if price else Decimal("0")
        open_fee, close_fee = self._fees(exchange, spot_quantity, spot_entry, spot_price, perp_base, perp_entry, perp_mark)
        net = spot_pnl - open_fee - close_fee
        return CashCarryPositionRow(
            exchange=exchange,
            symbol=symbol,
            status=self._status(spot_quantity, perp_base),
            spot_quantity=q(spot_quantity, "0.000001"),
            spot_entry_price=q(spot_entry),
            spot_price=q(spot_price),
            spot_unrealized_pnl=q(spot_pnl),
            perp_side="none",
            perp_contracts=Decimal("0"),
            perp_base_quantity=Decimal("0"),
            contract_size=contract_size,
            perp_entry_price=perp_entry,
            perp_mark_price=q(perp_mark),
            leverage=settings.default_leverage,
            perp_unrealized_pnl=Decimal("0"),
            estimated_funding_rate_pct=q(funding_rate_pct),
            estimated_funding_income=Decimal("0"),
            estimated_open_fee=q(open_fee),
            estimated_close_fee=q(close_fee),
            current_net_profit=q(net),
            quantity_gap=q(spot_quantity, "0.000001"),
            basis_pct=q(((perp_mark - spot_price) / spot_price * Decimal("100")) if spot_price > 0 and perp_mark > 0 else Decimal("0")),
            add_count=self._add_count(state),
            add_notional_usdt=self._add_notional(state, settings),
            next_add_trigger_basis_pct=self._next_add_trigger_basis(state, settings),
            updated_at=datetime.now(timezone.utc),
        )

    def _exchange(self, exchange_name: ExchangeName, default_type: str):
        exchange_id = SPOT_EXCHANGE_IDS[exchange_name] if default_type == "spot" else SWAP_EXCHANGE_IDS[exchange_name]
        return build_ccxt_exchange(exchange_name, exchange_id, default_type, timeout=12000)

    def _cached_spot_quantity(self, exchange: ExchangeName, base: str) -> Decimal:
        key = (exchange, base)
        cached = self._spot_balance_cache.get(key)
        if cached and time.monotonic() - cached[0] < 10:
            return cached[1]
        spot = self._exchange(exchange, "spot")
        quantity = self._spot_quantity(spot.fetch_balance({"type": "spot"}), base)
        self._spot_balance_cache[key] = (time.monotonic(), quantity)
        return quantity

    def _spot_quantity(self, balance, base: str) -> Decimal:
        item = balance.get(base, {}) if isinstance(balance, dict) else {}
        return decimal_from(item.get("total") or item.get("free"))

    def _cached_contract_size(self, exchange: ExchangeName, symbol: str) -> Decimal:
        key = (exchange, symbol)
        cached = self._contract_size_cache.get(key)
        if cached and time.monotonic() - cached[0] < 600:
            return cached[1]
        size = self._contract_size(self._exchange(exchange, "swap"), symbol)
        self._contract_size_cache[key] = (time.monotonic(), size)
        return size

    def _contract_size(self, exchange, symbol: str) -> Decimal:
        exchange.load_markets()
        return Decimal(str(exchange.market(symbol).get("contractSize") or "1"))

    def _cached_spot_price(self, exchange: ExchangeName, symbol: str, base: str) -> Decimal:
        if self.ticker_cache:
            self.ticker_cache.subscribe(exchange, "spot", symbol, f"{base}/USDT")
            ticker = self.ticker_cache.get(exchange, "spot", symbol)
            price = decimal_from((ticker or {}).get("last") or (ticker or {}).get("bid") or (ticker or {}).get("ask"))
            if price > 0:
                return price
        spot = self._exchange(exchange, "spot")
        return decimal_from(spot.fetch_ticker(f"{base}/USDT").get("last"))

    def _close_prices(self, exchange: ExchangeName, symbol: str, base: str, price: CashCarryOpportunity | None, fallback_perp: Decimal) -> tuple[Decimal, Decimal]:
        spot = self._exit_price(exchange, "spot", symbol, f"{base}/USDT", ("bid", "last", "close"), price.spot_price if price else Decimal("0"))
        swap = self._exit_price(exchange, "swap", symbol, f"{base}/USDT:USDT", ("ask", "last", "close"), fallback_perp)
        return spot, swap

    def _exit_price(self, exchange: ExchangeName, market_type: str, symbol: str, ccxt_symbol: str, keys: tuple[str, ...], fallback: Decimal) -> Decimal:
        if self.ticker_cache:
            self.ticker_cache.subscribe(exchange, market_type, symbol, ccxt_symbol)
            price = self._ticker_price(self.ticker_cache.get(exchange, market_type, symbol), keys)
            if price > 0:
                return price
        key = (exchange, market_type, symbol)
        cached = self._exit_price_cache.get(key)
        if cached and time.monotonic() - cached[0] < 2:
            return cached[1]
        try:
            price = self._ticker_price(self._exchange(exchange, market_type).fetch_ticker(ccxt_symbol), keys)
        except Exception:
            price = Decimal("0")
        result = price if price > 0 else fallback
        self._exit_price_cache[key] = (time.monotonic(), result)
        return result

    def _ticker_price(self, ticker: dict | None, keys: tuple[str, ...]) -> Decimal:
        for key in keys:
            price = decimal_from((ticker or {}).get(key))
            if price > 0:
                return price
        return Decimal("0")

    def _status(self, spot_quantity: Decimal, perp_base: Decimal) -> str:
        if spot_quantity <= 0 and perp_base > 0:
            return "perp_only"
        if spot_quantity > 0 and perp_base <= 0:
            return "spot_only"
        tolerance = max(Decimal("0.01"), max(abs(spot_quantity), abs(perp_base)) * Decimal("0.01"))
        return "matched" if abs(spot_quantity - perp_base) <= tolerance else "mismatch"

    def _state_records(self) -> dict[tuple[ExchangeName, str], dict[str, Any]]:
        path = Path(__file__).resolve().parents[3] / "config" / "cash_carry_execution_state.json"
        if not path.exists():
            return {}
        try:
            items = json.loads(path.read_text(encoding="utf-8")).get("positions", [])
        except (OSError, json.JSONDecodeError):
            return {}
        records = {}
        for item in items:
            if item.get("status") == "closed":
                continue
            try:
                records[(ExchangeName(item["exchange"]), item["symbol"])] = item
            except (KeyError, ValueError):
                continue
        return records

    def _entry_price(self, state: dict[str, Any] | None, key: str, fallback: Decimal) -> Decimal:
        price = decimal_from((state or {}).get(key))
        return price if price > 0 else fallback

    def _add_count(self, state: dict[str, Any] | None) -> int:
        try:
            return int((state or {}).get("add_count") or 0)
        except (TypeError, ValueError):
            return 0

    def _add_notional(self, state: dict[str, Any] | None, settings: BotSettings) -> Decimal:
        return settings.add_notional_usdt if self._add_count(state) < settings.max_add_count else Decimal("0")

    def _next_add_trigger_basis(self, state: dict[str, Any] | None, settings: BotSettings) -> Decimal | None:
        if not state or self._add_count(state) >= settings.max_add_count or settings.add_trigger_spread_pct <= 0:
            return None
        reference = decimal_from(state.get("last_add_basis_pct"))
        if reference <= 0:
            spot_entry = decimal_from(state.get("spot_entry_price"))
            perp_entry = decimal_from(state.get("perp_entry_price"))
            reference = (perp_entry - spot_entry) / spot_entry * Decimal("100") if spot_entry > 0 else Decimal("0")
        return q(reference + settings.add_trigger_spread_pct)

    def _fees(
        self,
        exchange: ExchangeName,
        spot_quantity: Decimal,
        spot_entry: Decimal,
        spot_price: Decimal,
        perp_base: Decimal,
        perp_entry: Decimal,
        perp_mark: Decimal,
    ) -> tuple[Decimal, Decimal]:
        rate = FEE_RATES[exchange]
        open_fee = (spot_quantity * spot_entry + perp_base * perp_entry) * rate
        close_fee = (spot_quantity * spot_price + perp_base * perp_mark) * rate
        return open_fee, close_fee

    def _funding_income(self, side: str, perp_base: Decimal, mark_price: Decimal, funding_rate_pct: Decimal) -> Decimal:
        direction = Decimal("1") if side == "short" else Decimal("-1")
        return perp_base * mark_price * funding_rate_pct / Decimal("100") * direction

    def _perp_unrealized(
        self,
        side: str,
        perp_base: Decimal,
        entry_price: Decimal,
        mark_price: Decimal,
        fallback: Decimal,
    ) -> Decimal:
        if perp_base <= 0 or entry_price <= 0 or mark_price <= 0:
            return fallback
        direction = Decimal("1") if side == "long" else Decimal("-1")
        return (mark_price - entry_price) * perp_base * direction
