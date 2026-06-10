from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.services.live_read import decimal_from


NETWORK_ALIASES = {
    "ARBITRUMONE": "ARBITRUM",
    "BEP20": "BSC",
    "BSCBEP20": "BSC",
    "ERC20": "ETH",
    "ETHEREUM": "ETH",
    "OP": "OPTIMISM",
    "TRC20": "TRX",
}


@dataclass
class TransferNetworks:
    deposit: set[str] = field(default_factory=set)
    withdraw: set[str] = field(default_factory=set)


@dataclass
class RouteCheck:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def extract_transfer_networks(currencies: dict[str, Any]) -> dict[str, TransferNetworks]:
    result: dict[str, TransferNetworks] = {}
    for code, currency in currencies.items():
        networks = currency.get("networks") if isinstance(currency, dict) else None
        if not isinstance(networks, dict):
            continue
        entry = TransferNetworks()
        for name, network in networks.items():
            normalized = normalize_network(str(network.get("network") or name))
            if not normalized:
                continue
            active = network.get("active")
            if active is False:
                continue
            if network.get("deposit") is not False:
                entry.deposit.add(normalized)
            if network.get("withdraw") is not False:
                entry.withdraw.add(normalized)
        if entry.deposit or entry.withdraw:
            result[str(code).upper()] = entry
    return result


def normalize_network(network: str) -> str:
    value = "".join(ch for ch in network.upper() if ch.isalnum())
    return NETWORK_ALIASES.get(value, value)


def has_bidirectional_route(base: str, left: dict[str, TransferNetworks], right: dict[str, TransferNetworks]) -> bool:
    return bidirectional_route_check(base, left, right).ok


def bidirectional_route_check(
    base: str,
    left: dict[str, TransferNetworks],
    right: dict[str, TransferNetworks],
    left_label: str = "左侧交易所",
    right_label: str = "右侧交易所",
    left_query_ok: bool = False,
    right_query_ok: bool = False,
) -> RouteCheck:
    left_networks = left.get(base.upper())
    right_networks = right.get(base.upper())
    if not left_networks and not right_networks:
        if left_query_ok and right_query_ok:
            return RouteCheck(False, [f"{base}: 两边币种链路接口已成功返回，但均未列出该币可充提链，按当前接口确认无可用现货互通链路"])
        return RouteCheck(False, [f"{base}: 两边币种充值/提现链路数据均未返回，属于未确认，不等于确认没有"])
    if not left_networks:
        if left_query_ok:
            return RouteCheck(False, [f"{base}: {left_label} 链路接口已成功返回，但未列出该币可充提链，按当前接口确认该侧不可作为现货互通链路"])
        return RouteCheck(False, [f"{base}: {left_label} 未返回币种充值/提现链路数据，属于未确认"])
    if not right_networks:
        if right_query_ok:
            return RouteCheck(False, [f"{base}: {right_label} 链路接口已成功返回，但未列出该币可充提链，按当前接口确认该侧不可作为现货互通链路"])
        return RouteCheck(False, [f"{base}: {right_label} 未返回币种充值/提现链路数据，属于未确认"])
    left_to_right = left_networks.withdraw & right_networks.deposit
    right_to_left = right_networks.withdraw & left_networks.deposit
    reasons = []
    if not left_to_right:
        reasons.append(f"{base}: 已确认 {left_label} 可提现链与 {right_label} 可充值链无交集")
    if not right_to_left:
        reasons.append(f"{base}: 已确认 {right_label} 可提现链与 {left_label} 可充值链无交集")
    return RouteCheck(not reasons, reasons)


def has_depth(exchange, symbol: str, side: str, price: Decimal, notional: Decimal, slippage_pct: Decimal) -> bool:
    if not exchange.has.get("fetchOrderBook"):
        return False
    orderbook = exchange.fetch_order_book(symbol, limit=50)
    levels = orderbook.get("asks" if side == "long" else "bids") or []
    if not levels:
        return False
    limit_price = price * (Decimal("1") + slippage_pct / Decimal("100")) if side == "long" else price * (Decimal("1") - slippage_pct / Decimal("100"))
    total = Decimal("0")
    for level in levels:
        level_price = decimal_from(level[0])
        amount = decimal_from(level[1])
        if side == "long" and level_price > limit_price:
            break
        if side == "short" and level_price < limit_price:
            break
        total += level_price * amount
        if total >= notional:
            return True
    return False
