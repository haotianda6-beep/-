from decimal import Decimal
import time
from typing import Any


def contract_order_amount(exchange, symbol: str, base_quantity: Decimal) -> float:
    exchange.load_markets()
    market = exchange.market(symbol)
    contract_size = Decimal(str(market.get("contractSize") or "1"))
    raw_amount = base_quantity / contract_size if contract_size > 0 else base_quantity
    return float(exchange.amount_to_precision(symbol, float(raw_amount)))


def spot_market_buy(exchange, symbol: str, quote_cost: Decimal, fallback_base_quantity: Decimal, params: dict[str, Any] | None = None):
    order_params = params or {}
    if hasattr(exchange, "create_market_buy_order_with_cost"):
        return exchange.create_market_buy_order_with_cost(symbol, float(quote_cost), order_params)
    return exchange.create_order(symbol, "market", "buy", float(fallback_base_quantity), None, order_params)


def filled_base_quantity(exchange, symbol: str, order, fallback: Decimal) -> Decimal:
    current = fetch_order_snapshot(exchange, symbol, order)
    if isinstance(current, dict):
        base = symbol.split("/", 1)[0]
        cost = current.get("cost")
        average = current.get("average")
        if cost not in (None, "") and average not in (None, ""):
            avg = Decimal(str(average))
            if avg > 0:
                amount = _after_base_fee(Decimal(str(cost)) / avg, current, base)
                if amount > 0:
                    return amount
        for key in ("filled", "amount"):
            value = current.get(key)
            if value not in (None, ""):
                amount = _after_base_fee(Decimal(str(value)), current, base)
                if amount > 0:
                    return amount
    return fallback


def fetch_order_snapshot(exchange, symbol: str, order, attempts: int = 3, delay_seconds: float = 0.2):
    if not isinstance(order, dict) or not order.get("id"):
        return order
    if not getattr(exchange, "has", {}).get("fetchOrder"):
        return order
    current = order
    if _has_fill_snapshot(current):
        return current
    for attempt in range(max(attempts, 1)):
        try:
            fetched = exchange.fetch_order(order["id"], symbol)
            if isinstance(fetched, dict):
                current = {**order, **fetched}
                if _has_fill_snapshot(current):
                    return current
        except Exception:  # noqa: BLE001
            current = order
        if _has_fill_snapshot(current):
            return current
        if attempt < attempts - 1 and delay_seconds > 0:
            time.sleep(delay_seconds)
    return current


def order_average_price(order, fallback: Decimal) -> Decimal:
    if not isinstance(order, dict):
        return fallback
    for key in ("average", "price"):
        value = order.get(key)
        if value not in (None, "", 0):
            price = Decimal(str(value))
            if price > 0:
                return price
    cost = order.get("cost")
    amount = order.get("filled") or order.get("amount")
    if cost not in (None, "") and amount not in (None, ""):
        quantity = Decimal(str(amount))
        if quantity > 0:
            return Decimal(str(cost)) / quantity
    return fallback


def _after_base_fee(amount: Decimal, order: dict[str, Any], base: str) -> Decimal:
    fee_total = Decimal("0")
    for fee in order.get("fees") or []:
        if isinstance(fee, dict) and str(fee.get("currency", "")).upper() == base.upper():
            fee_total += Decimal(str(fee.get("cost") or "0"))
    fee = order.get("fee")
    if isinstance(fee, dict) and str(fee.get("currency", "")).upper() == base.upper():
        fee_total += Decimal(str(fee.get("cost") or "0"))
    return amount - fee_total


def _has_fill_snapshot(order: dict[str, Any]) -> bool:
    if order.get("average") not in (None, "", 0) and order.get("cost") not in (None, "", 0):
        return True
    return any(order.get(key) not in (None, "", 0) for key in ("filled", "amount"))
