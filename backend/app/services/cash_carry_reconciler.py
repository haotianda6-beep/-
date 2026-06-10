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


def _build(spot, swap, record, spot_symbol, swap_symbol, close_spot_order_id, close_perp_order_id) -> dict[str, Any]:
    if not all([record.spot_order_id, record.perp_order_id, close_spot_order_id, close_perp_order_id]):
        return {}
    since = int((record.opened_at - timedelta(minutes=5)).timestamp() * 1000)
    closed_at = datetime.now(record.opened_at.tzinfo)
    spot_trades = _by_order(spot.fetch_my_trades(spot_symbol, since=since, limit=100), {record.spot_order_id, close_spot_order_id})
    swap_trades = _by_order(swap.fetch_my_trades(swap_symbol, since=since, limit=100), {record.perp_order_id, close_perp_order_id})
    spot_open = _spot_group(spot_trades.get(record.spot_order_id, []))
    spot_close = _spot_group(spot_trades.get(close_spot_order_id, []))
    if hasattr(swap, "load_markets"):
        swap.load_markets()
    contract_size = Decimal(str(swap.market(swap_symbol).get("contractSize") or "1"))
    perp_open = _perp_group(swap_trades.get(record.perp_order_id, []), contract_size)
    perp_close = _perp_group(swap_trades.get(close_perp_order_id, []), contract_size)
    if not all([spot_open["qty"], spot_close["qty"], perp_open["qty"], perp_close["qty"]]):
        return {}
    funding = _funding(swap, swap_symbol, since, closed_at)
    fee = spot_open["fee"] + spot_close["fee"] + perp_open["fee"] + perp_close["fee"]
    long_pnl = (spot_close["avg"] - spot_open["avg"]) * spot_close["qty"]
    short_pnl = (perp_open["avg"] - perp_close["avg"]) * perp_close["qty"]
    total = long_pnl + short_pnl
    return {"opened_at": record.opened_at.isoformat(), "quantity": str(min(spot_close["qty"], perp_close["qty"])), "long_open_price": str(spot_open["avg"]), "long_close_price": str(spot_close["avg"]), "short_open_price": str(perp_open["avg"]), "short_close_price": str(perp_close["avg"]), "actual_fee": str(fee), "total_pnl": str(total), "long_pnl": str(long_pnl), "short_pnl": str(short_pnl), "funding_net": str(funding), "actual_net_profit": str(total + funding - fee), "long_order_ids": [record.spot_order_id, close_spot_order_id], "short_order_ids": [record.perp_order_id, close_perp_order_id], "reconcile_status": "verified"}


def _by_order(trades: list[dict[str, Any]], ids: set[str | None]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        order_id = str(trade.get("order") or "")
        if order_id in ids:
            grouped.setdefault(order_id, []).append(trade)
    return grouped


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
