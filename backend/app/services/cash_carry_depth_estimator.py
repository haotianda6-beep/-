from decimal import Decimal

from app.core.models import BotSettings


def estimate_max_safe_notional(
    spot,
    swap,
    spot_symbol: str,
    swap_symbol: str,
    settings: BotSettings,
    spot_fee: Decimal,
    swap_fee: Decimal,
    funding_rate: Decimal,
) -> Decimal | None:
    try:
        spot_book = spot.fetch_order_book(spot_symbol, limit=50)
        if hasattr(swap, "load_markets"):
            swap.load_markets()
        contract_size = Decimal(str(swap.market(swap_symbol).get("contractSize") or "1"))
        swap_book = swap.fetch_order_book(swap_symbol, limit=50)
    except Exception:
        return None
    max_slippage = settings.max_slippage_pct
    min_net = settings.min_funding_net_usdt
    max_notional = _forward_depth_notional(spot_book, swap_book, contract_size)
    check = lambda notional: _forward_ok(  # noqa: E731
        spot_book,
        swap_book,
        contract_size,
        notional,
        max_slippage,
        settings.cash_carry_min_basis_pct,
        min_net,
        spot_fee,
        swap_fee,
        funding_rate,
    )
    if max_notional <= 0 or not check(min(settings.order_notional_usdt, max_notional)):
        return Decimal("0")
    if check(max_notional):
        return max_notional
    low = Decimal("0")
    high = max_notional
    for _ in range(24):
        mid = (low + high) / Decimal("2")
        if check(mid):
            low = mid
        else:
            high = mid
    return low


def _forward_ok(
    spot_book: dict,
    swap_book: dict,
    contract_size: Decimal,
    notional: Decimal,
    max_slippage_pct: Decimal,
    min_basis_pct: Decimal,
    min_net_profit: Decimal,
    spot_fee: Decimal,
    swap_fee: Decimal,
    funding_rate: Decimal,
) -> bool:
    spot_qty, spot_avg = _buy_vwap_from_quote(spot_book.get("asks") or [], notional, Decimal("1"))
    _, swap_avg = _sell_vwap_base(swap_book.get("bids") or [], spot_qty, contract_size)
    if spot_qty <= 0 or spot_avg <= 0 or swap_avg <= 0:
        return False
    top_spot = _top_price(spot_book.get("asks") or [])
    top_swap = _top_price(swap_book.get("bids") or [])
    if top_spot <= 0 or top_swap <= 0:
        return False
    basis_pct = (swap_avg - spot_avg) / spot_avg * Decimal("100")
    if basis_pct < min_basis_pct:
        return False
    spot_slippage = (spot_avg - top_spot) / top_spot * Decimal("100")
    swap_slippage = (top_swap - swap_avg) / top_swap * Decimal("100")
    if spot_slippage > max_slippage_pct or swap_slippage > max_slippage_pct:
        return False
    basis_profit = (swap_avg - spot_avg) * spot_qty
    fees = (spot_qty * spot_avg * spot_fee + spot_qty * swap_avg * swap_fee) * Decimal("2")
    net_profit = basis_profit + notional * funding_rate - fees
    return net_profit >= min_net_profit


def _forward_depth_notional(spot_book: dict, swap_book: dict, contract_size: Decimal) -> Decimal:
    quote = sum(Decimal(str(price)) * Decimal(str(amount)) for price, amount, *_ in spot_book.get("asks") or [])
    swap_base = sum(Decimal(str(amount)) * contract_size for _price, amount, *_ in swap_book.get("bids") or [])
    top_spot = _top_price(spot_book.get("asks") or [])
    return min(quote, swap_base * top_spot) if top_spot > 0 else Decimal("0")


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


def _top_price(levels: list) -> Decimal:
    if not levels:
        return Decimal("0")
    return Decimal(str(levels[0][0]))
