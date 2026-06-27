import time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.services.cash_carry_execution_models import CashCarryPosition


def build_cash_carry_history(
    spot,
    swap,
    record: CashCarryPosition,
    spot_symbol: str,
    swap_symbol: str,
    close_spot_order_id: str | None,
    close_perp_order_id: str | None,
) -> dict[str, Any]:
    for attempt in range(3):
        try:
            history = _build(spot, swap, record, spot_symbol, swap_symbol, close_spot_order_id, close_perp_order_id)
            if history or attempt == 2:
                return history
        except Exception:  # noqa: BLE001 - reconciliation must not block protective close.
            if attempt == 2:
                return {}
        time.sleep(1)
    return {}


def build_cash_carry_external_perp_close_history(
    spot,
    swap,
    record: CashCarryPosition,
    spot_symbol: str,
    swap_symbol: str,
) -> dict[str, Any]:
    for attempt in range(3):
        try:
            history = _build_external_perp_close(spot, swap, record, spot_symbol, swap_symbol)
            if history or attempt == 2:
                return history
        except Exception:  # noqa: BLE001 - reconciliation must not block live monitoring.
            if attempt == 2:
                return {}
        time.sleep(1)
    return {}


def _build(spot, swap, record, spot_symbol, swap_symbol, close_spot_order_id, close_perp_order_id) -> dict[str, Any]:
    if not all([record.spot_order_id, record.perp_order_id, close_spot_order_id, close_perp_order_id]):
        return {}
    spot_open_ids = _open_spot_order_ids(record)
    perp_open_ids = _open_perp_order_ids(record)
    since = int((record.opened_at - timedelta(minutes=5)).timestamp() * 1000)
    closed_at = datetime.now(record.opened_at.tzinfo)
    spot_trades = _by_order(spot.fetch_my_trades(spot_symbol, since=since, limit=100), {*spot_open_ids, close_spot_order_id})
    swap_trades = _by_order(swap.fetch_my_trades(swap_symbol, since=since, limit=100), {*perp_open_ids, close_perp_order_id})
    spot_open = _spot_group(_ordered_group(spot_trades, spot_open_ids))
    spot_close = _spot_group(spot_trades.get(close_spot_order_id, []))
    if hasattr(swap, "load_markets"):
        swap.load_markets()
    contract_size = Decimal(str(swap.market(swap_symbol).get("contractSize") or "1"))
    perp_open = _perp_group(_ordered_group(swap_trades, perp_open_ids), contract_size)
    perp_close = _perp_group(swap_trades.get(close_perp_order_id, []), contract_size)
    if not all([spot_open["qty"], spot_close["qty"], perp_open["qty"], perp_close["qty"]]):
        return {}
    funding = _funding(swap, swap_symbol, since, closed_at)
    fee = spot_open["fee"] + spot_close["fee"] + perp_open["fee"] + perp_close["fee"]
    long_pnl = (spot_close["avg"] - spot_open["avg"]) * spot_close["qty"]
    short_pnl = (perp_open["avg"] - perp_close["avg"]) * perp_close["qty"]
    total = long_pnl + short_pnl
    return {"opened_at": record.opened_at.isoformat(), "quantity": str(min(spot_close["qty"], perp_close["qty"])), "long_open_price": str(spot_open["avg"]), "long_close_price": str(spot_close["avg"]), "short_open_price": str(perp_open["avg"]), "short_close_price": str(perp_close["avg"]), "actual_fee": str(fee), "total_pnl": str(total), "long_pnl": str(long_pnl), "short_pnl": str(short_pnl), "funding_net": str(funding), "actual_net_profit": str(total + funding - fee), "long_order_ids": [*spot_open_ids, close_spot_order_id], "short_order_ids": [*perp_open_ids, close_perp_order_id], "reconcile_status": "verified"}


def _build_external_perp_close(spot, swap, record, spot_symbol, swap_symbol) -> dict[str, Any]:
    if not all([record.spot_order_id, record.perp_order_id]):
        return {}
    spot_open_ids = _open_spot_order_ids(record)
    perp_open_ids = _open_perp_order_ids(record)
    since = int((record.opened_at - timedelta(minutes=5)).timestamp() * 1000)
    spot_trades = _by_order(spot.fetch_my_trades(spot_symbol, since=since, limit=100), set(spot_open_ids))
    all_swap_trades = swap.fetch_my_trades(swap_symbol, since=since, limit=100)
    swap_trades = _by_order(all_swap_trades, set(perp_open_ids))
    close_trades = _external_close_trades(all_swap_trades, set(perp_open_ids), record.opened_at)
    spot_open = _spot_group(_ordered_group(spot_trades, spot_open_ids))
    if hasattr(swap, "load_markets"):
        swap.load_markets()
    contract_size = Decimal(str(swap.market(swap_symbol).get("contractSize") or "1"))
    perp_open = _perp_group(_ordered_group(swap_trades, perp_open_ids), contract_size)
    perp_close = _perp_group(close_trades, contract_size)
    if not all([spot_open["qty"], perp_open["qty"], perp_close["qty"]]):
        return {}
    closed_at = _close_time(close_trades, record.opened_at)
    funding = _funding(swap, swap_symbol, since, closed_at)
    fee = spot_open["fee"] + perp_open["fee"] + perp_close["fee"]
    quantity = min(spot_open["qty"], perp_close["qty"])
    short_pnl = (perp_open["avg"] - perp_close["avg"]) * perp_close["qty"]
    close_order_ids = _ordered_ids(close_trades)
    close_type = "liquidation" if _forced_close(close_trades) else "external_close"
    return {
        "opened_at": record.opened_at.isoformat(),
        "closed_at": closed_at.isoformat(),
        "quantity": str(quantity),
        "long_open_price": str(spot_open["avg"]),
        "long_close_price": None,
        "short_open_price": str(perp_open["avg"]),
        "short_close_price": str(perp_close["avg"]),
        "actual_fee": str(fee),
        "total_pnl": str(short_pnl),
        "long_pnl": "0",
        "short_pnl": str(short_pnl),
        "funding_net": str(funding),
        "actual_net_profit": str(short_pnl + funding - fee),
        "long_order_ids": spot_open_ids,
        "short_order_ids": [*perp_open_ids, *close_order_ids],
        "close_perp_order_id": close_order_ids[-1] if close_order_ids else None,
        "reconcile_status": "verified",
        "external_close_type": close_type,
    }


def _by_order(trades: list[dict[str, Any]], ids: set[str | None]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        order_id = str(trade.get("order") or "")
        if order_id in ids:
            grouped.setdefault(order_id, []).append(trade)
    return grouped


def _external_close_trades(trades: list[dict[str, Any]], open_order_ids: set[str], opened_at: datetime) -> list[dict[str, Any]]:
    opened_ms = int(opened_at.timestamp() * 1000)
    result = []
    for trade in trades:
        if str(trade.get("order") or "") in open_order_ids:
            continue
        if str(trade.get("side") or "").lower() != "buy":
            continue
        timestamp = trade.get("timestamp")
        if timestamp and int(timestamp) < opened_ms:
            continue
        result.append(trade)
    return result


def _open_spot_order_ids(record: CashCarryPosition) -> list[str]:
    return _dedupe_ids([record.spot_order_id, *(_add_order_ids(record, "spot_order_id"))])


def _open_perp_order_ids(record: CashCarryPosition) -> list[str]:
    return _dedupe_ids([record.perp_order_id, *(_add_order_ids(record, "perp_order_id"))])


def _add_order_ids(record: CashCarryPosition, key: str) -> list[str | None]:
    return [str(item.get(key)) for item in record.add_orders if isinstance(item, dict) and item.get(key)]


def _dedupe_ids(ids: list[str | None]) -> list[str]:
    result = []
    for item in ids:
        value = str(item or "")
        if value and value not in result:
            result.append(value)
    return result


def _ordered_group(grouped: dict[str, list[dict[str, Any]]], order_ids: list[str]) -> list[dict[str, Any]]:
    rows = []
    for order_id in order_ids:
        rows.extend(grouped.get(order_id, []))
    return rows


def _ordered_ids(trades: list[dict[str, Any]]) -> list[str]:
    seen = []
    for trade in trades:
        order_id = str(trade.get("order") or "")
        if order_id and order_id not in seen:
            seen.append(order_id)
    return seen


def _close_time(trades: list[dict[str, Any]], fallback: datetime) -> datetime:
    timestamps = [int(trade["timestamp"]) for trade in trades if trade.get("timestamp")]
    if not timestamps:
        return datetime.now(fallback.tzinfo)
    return datetime.fromtimestamp(max(timestamps) / 1000, tz=fallback.tzinfo)


def _forced_close(trades: list[dict[str, Any]]) -> bool:
    text = " ".join(str(trade.get("info") or "").lower() for trade in trades)
    return "burst" in text or "liquid" in text or "force" in text or "liq-" in text


def _spot_group(trades: list[dict[str, Any]]) -> dict[str, Decimal]:
    qty = sum(_dec(item.get("amount")) for item in trades)
    cost = sum(_dec(item.get("cost")) for item in trades)
    avg = cost / qty if qty > 0 else Decimal("0")
    fee = sum(_fee_usdt(item, avg) for item in trades)
    return {"qty": qty, "cost": cost, "avg": avg, "fee": fee}


def _perp_group(trades: list[dict[str, Any]], contract_size: Decimal) -> dict[str, Decimal]:
    contracts = sum(_dec(item.get("amount")) for item in trades)
    qty = contracts * contract_size
    cost = sum(_dec(item.get("cost")) for item in trades)
    avg = cost / qty if qty > 0 else Decimal("0")
    fee = sum(_fee_usdt(item, avg) for item in trades)
    return {"qty": qty, "cost": cost, "avg": avg, "fee": fee}


def _fee_usdt(trade: dict[str, Any], price: Decimal) -> Decimal:
    fee = trade.get("fee") or {}
    cost = _dec(fee.get("cost"))
    return cost * price if fee.get("currency") not in {None, "USDT"} else cost


def _funding(swap, symbol: str, since: int, closed_at: datetime) -> Decimal:
    if not getattr(swap, "has", {}).get("fetchFundingHistory"):
        return Decimal("0")
    total = Decimal("0")
    for item in swap.fetch_funding_history(symbol, since=since, limit=100):
        ts = item.get("timestamp")
        if ts and datetime.fromtimestamp(ts / 1000, tz=closed_at.tzinfo) <= closed_at:
            total += _dec(item.get("amount"))
    return total


def _dec(value) -> Decimal:
    return Decimal(str(value or "0"))
