from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class DepthGuardResult:
    ok: bool
    reason: str
    spot_price: Decimal = Decimal("0")
    perp_price: Decimal = Decimal("0")
    basis_pct: Decimal = Decimal("0")
    estimated_net_profit: Decimal = Decimal("0")


def forward_open_depth_guard(
    spot,
    swap,
    spot_symbol: str,
    swap_symbol: str,
    quote_notional: Decimal,
    min_basis_pct: Decimal,
    *,
    min_net_profit: Decimal = Decimal("0"),
    open_close_fee: Decimal = Decimal("0"),
    funding_income: Decimal = Decimal("0"),
    close_basis_pct: Decimal = Decimal("0"),
) -> DepthGuardResult:
    try:
        spot_book = spot.fetch_order_book(spot_symbol, limit=20)
        spot_qty, spot_avg = _buy_vwap_from_quote(spot_book.get("asks") or [], quote_notional, Decimal("1"))
        if spot_qty <= 0 or spot_avg <= 0:
            return DepthGuardResult(False, "现货卖盘深度不足，无法按单笔金额买入")
        if hasattr(swap, "load_markets"):
            swap.load_markets()
        contract_size = Decimal(str(swap.market(swap_symbol).get("contractSize") or "1"))
        swap_book = swap.fetch_order_book(swap_symbol, limit=20)
        _, perp_avg = _sell_vwap_base(swap_book.get("bids") or [], spot_qty, contract_size)
        if perp_avg <= 0:
            return DepthGuardResult(False, "合约买盘深度不足，无法按现货数量做空")
        basis = (perp_avg - spot_avg) / spot_avg * Decimal("100") if spot_avg > 0 else Decimal("0")
        if basis < min_basis_pct:
            return DepthGuardResult(False, f"深度均价开仓基差 {basis:.4f}% 低于 {min_basis_pct}% ", spot_avg, perp_avg, basis)
        tradable_basis = max(Decimal("0"), basis - close_basis_pct)
        estimated_net = quote_notional * tradable_basis / Decimal("100") + funding_income - open_close_fee
        if estimated_net < min_net_profit:
            return DepthGuardResult(
                False,
                f"深度均价净利 {estimated_net:.4f} USDT 低于稳定开仓安全垫 {min_net_profit:.4f} USDT",
                spot_avg,
                perp_avg,
                basis,
                estimated_net,
            )
        return DepthGuardResult(True, "ok", spot_avg, perp_avg, basis, estimated_net)
    except Exception as exc:  # noqa: BLE001
        return DepthGuardResult(False, f"开仓深度校验失败 {str(exc)[:160]}")


def forward_close_depth_guard(
    spot,
    swap,
    spot_symbol: str,
    swap_symbol: str,
    spot_quantity: Decimal,
    perp_base_quantity: Decimal,
    spot_entry_price: Decimal,
    perp_entry_price: Decimal,
    fee_rate: Decimal,
    min_net_profit: Decimal,
) -> DepthGuardResult:
    try:
        spot_book = spot.fetch_order_book(spot_symbol, limit=20)
        _, spot_avg = _sell_vwap_base(spot_book.get("bids") or [], spot_quantity, Decimal("1"))
        if spot_avg <= 0:
            return DepthGuardResult(False, "现货买盘深度不足，无法按持仓数量卖出")
        if hasattr(swap, "load_markets"):
            swap.load_markets()
        contract_size = Decimal(str(swap.market(swap_symbol).get("contractSize") or "1"))
        swap_book = swap.fetch_order_book(swap_symbol, limit=20)
        _, perp_avg = _buy_vwap_base(swap_book.get("asks") or [], perp_base_quantity, contract_size)
        if perp_avg <= 0:
            return DepthGuardResult(False, "合约卖盘深度不足，无法按持仓数量平空")
        basis = (perp_avg - spot_avg) / spot_avg * Decimal("100") if spot_avg > 0 else Decimal("0")
        gross_pnl = (spot_avg - spot_entry_price) * spot_quantity + (perp_entry_price - perp_avg) * perp_base_quantity
        open_fee = (spot_quantity * spot_entry_price + perp_base_quantity * perp_entry_price) * fee_rate
        close_fee = (spot_quantity * spot_avg + perp_base_quantity * perp_avg) * fee_rate
        net_profit = gross_pnl - open_fee - close_fee
        if net_profit < min_net_profit:
            return DepthGuardResult(
                False,
                f"盘口可成交净利 {net_profit:.4f} USDT 低于平仓安全垫 {min_net_profit:.4f} USDT",
                spot_avg,
                perp_avg,
                basis,
                net_profit,
            )
        return DepthGuardResult(True, "ok", spot_avg, perp_avg, basis, net_profit)
    except Exception as exc:  # noqa: BLE001
        return DepthGuardResult(False, f"平仓深度校验失败 {str(exc)[:160]}")


def _buy_vwap_from_quote(levels: list, quote_notional: Decimal, unit_size: Decimal) -> tuple[Decimal, Decimal]:
    remaining = quote_notional
    base_qty = Decimal("0")
    quote_cost = Decimal("0")
    for price_raw, amount_raw, *_ in levels:
        price = Decimal(str(price_raw))
        base_available = Decimal(str(amount_raw)) * unit_size
        quote_available = base_available * price
        quote_take = min(remaining, quote_available)
        if price <= 0 or quote_take <= 0:
            continue
        base_take = quote_take / price
        base_qty += base_take
        quote_cost += quote_take
        remaining -= quote_take
        if remaining <= 0:
            break
    return base_qty, quote_cost / base_qty if base_qty > 0 and remaining <= 0 else Decimal("0")


def _sell_vwap_base(levels: list, base_quantity: Decimal, unit_size: Decimal) -> tuple[Decimal, Decimal]:
    remaining = base_quantity
    base_sold = Decimal("0")
    quote_received = Decimal("0")
    for price_raw, amount_raw, *_ in levels:
        price = Decimal(str(price_raw))
        base_available = Decimal(str(amount_raw)) * unit_size
        base_take = min(remaining, base_available)
        if price <= 0 or base_take <= 0:
            continue
        base_sold += base_take
        quote_received += base_take * price
        remaining -= base_take
        if remaining <= 0:
            break
    return base_sold, quote_received / base_sold if base_sold > 0 and remaining <= 0 else Decimal("0")


def _buy_vwap_base(levels: list, base_quantity: Decimal, unit_size: Decimal) -> tuple[Decimal, Decimal]:
    remaining = base_quantity
    base_bought = Decimal("0")
    quote_cost = Decimal("0")
    for price_raw, amount_raw, *_ in levels:
        price = Decimal(str(price_raw))
        base_available = Decimal(str(amount_raw)) * unit_size
        base_take = min(remaining, base_available)
        if price <= 0 or base_take <= 0:
            continue
        base_bought += base_take
        quote_cost += base_take * price
        remaining -= base_take
        if remaining <= 0:
            break
    return base_bought, quote_cost / base_bought if base_bought > 0 and remaining <= 0 else Decimal("0")
