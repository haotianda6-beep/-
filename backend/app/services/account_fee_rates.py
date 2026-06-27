import time
from decimal import Decimal
from typing import Any

from app.core.models import ExchangeName
from app.services.exchange_factory import sanitize_exchange_error
from app.services.live_read import decimal_from


FEE_CACHE_TTL_SECONDS = 600

_TAKER_FEE_CACHE: dict[tuple[ExchangeName, str], tuple[float, dict[str, Decimal]]] = {}


def clear_account_fee_cache() -> None:
    _TAKER_FEE_CACHE.clear()


def cached_account_taker_fee(exchange_name: ExchangeName, market_type: str, symbol: str) -> Decimal | None:
    cached = _TAKER_FEE_CACHE.get((ExchangeName(exchange_name), market_type))
    if not cached or time.monotonic() - cached[0] >= FEE_CACHE_TTL_SECONDS:
        return None
    return cached[1].get(symbol)


def account_taker_fee_map(
    exchange_name: ExchangeName,
    market_type: str,
    exchange: Any,
    issues: list[str] | None = None,
) -> dict[str, Decimal]:
    key = (ExchangeName(exchange_name), market_type)
    cached = _TAKER_FEE_CACHE.get(key)
    if cached and time.monotonic() - cached[0] < FEE_CACHE_TTL_SECONDS:
        return cached[1]
    if not getattr(exchange, "has", {}).get("fetchTradingFees"):
        return {}
    try:
        raw = exchange.fetch_trading_fees()
    except Exception as exc:  # noqa: BLE001
        if issues is not None:
            label = "现货" if market_type == "spot" else "合约"
            issues.append(f"{exchange_name}: {label}账户手续费读取失败，使用公开费率 {sanitize_exchange_error(str(exc))[:160]}")
        return {}
    fees = _parse_taker_fee_map(raw)
    _TAKER_FEE_CACHE[key] = (time.monotonic(), fees)
    return fees


def _parse_taker_fee_map(raw: Any) -> dict[str, Decimal]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Decimal] = {}
    for key, item in raw.items():
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or key)
        taker = decimal_from(item.get("taker"), "0")
        if symbol and taker > 0:
            result[symbol] = taker
    return result
