import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.core.models import BotSettings, CashCarryOpportunity, CashCarryPositionRow, ExchangeName
from app.services.cash_carry_add_policy import CashCarryAddDecision, cash_carry_add_decision
from app.services.cash_carry_execution_models import CashCarryPosition
from app.services.order_sizing import contract_order_amount, fetch_order_snapshot, filled_base_quantity, order_average_price, spot_market_buy
from app.services.execution_models import ExecutionResult, ExecutionStep


def evaluate_cash_carry_add(
    executor,
    rows: list[CashCarryOpportunity],
    settings: BotSettings,
    position_rows: list[CashCarryPositionRow] | None = None,
) -> ExecutionResult | None:
    if not settings.cash_carry_auto_open_enabled or settings.max_add_count <= 0 or settings.add_trigger_spread_pct <= 0:
        return None
    by_key = {(row.exchange, row.symbol): row for row in rows}
    live_by_key = {(ExchangeName(row.exchange), row.symbol): row for row in position_rows or []}
    records = executor.state.load_positions()
    total_notional = _active_notional(records, by_key)
    for record in records:
        current = _add_candidate(by_key.get((record.exchange, record.symbol)))
        live = live_by_key.get((record.exchange, record.symbol))
        if not current or not live or live.status != "matched":
            continue
        decision = cash_carry_add_decision(record, current, settings, total_notional)
        if not decision.should_add:
            continue
        steps = _add_plan(record, current, settings, decision)
        gate_reasons = executor._safety_gate(settings, opening=True)
        if gate_reasons:
            return executor.state.remember(ExecutionResult(str(uuid.uuid4()), "blocked_by_safety_gate", " / ".join(gate_reasons), steps))
        return _execute_add(executor, record, current, settings, steps)
    return None


def _add_candidate(item: CashCarryOpportunity | None) -> CashCarryOpportunity | None:
    if not item:
        return None
    reasons = [reason for reason in item.blocked_reasons if not _is_open_scope_reason(reason)]
    return item.model_copy(update={"blocked_reasons": reasons}) if reasons != item.blocked_reasons else item


def _is_open_scope_reason(reason: str) -> bool:
    return "一所一币规则" in reason or "已有正向期现持仓" in reason


def _execute_add(
    executor,
    record: CashCarryPosition,
    item: CashCarryOpportunity,
    settings: BotSettings,
    steps: list[ExecutionStep],
) -> ExecutionResult:
    spot = executor._exchange(item.exchange, "spot")
    swap = executor._exchange(item.exchange, "swap")
    spot_symbol = f"{record.base_asset}/USDT"
    swap_symbol = f"{record.base_asset}/USDT:USDT"
    base_qty = item.quantity
    try:
        executor._maybe_transfer(spot, item, settings, steps[0])
        spot_order_raw = executor._run(steps[1], lambda: spot_market_buy(spot, spot_symbol, settings.order_notional_usdt, item.quantity), True)
        spot_order = fetch_order_snapshot(spot, spot_symbol, spot_order_raw)
        base_qty = filled_base_quantity(spot, spot_symbol, spot_order, item.quantity)
        contract_qty = contract_order_amount(swap, swap_symbol, base_qty)
        executor._run(steps[2], lambda: executor._set_leverage(swap, swap_symbol, settings.default_leverage), True)
        perp_order_raw = executor._run(
            steps[3],
            lambda: swap.create_order(swap_symbol, "market", "sell", contract_qty, None, {"reduceOnly": False, "marginMode": settings.margin_mode}),
            True,
        )
        perp_order = fetch_order_snapshot(swap, swap_symbol, perp_order_raw)
        total_qty = record.quantity + base_qty
        spot_price = _order_decimal(executor, spot_order, item.spot_price)
        perp_price = _order_decimal(executor, perp_order, item.perp_price)
        executor.state.mark_added(
            record.id,
            total_qty,
            _weighted_entry(record.quantity, record.spot_entry_price, base_qty, spot_price),
            _weighted_entry(record.quantity, record.perp_entry_price, base_qty, perp_price),
            _add_fields(executor, spot_order, perp_order, base_qty, spot_price, perp_price, item.basis_pct),
        )
        return executor.state.remember(ExecutionResult(record.id, "add_submitted", "已提交正向期现补仓流程", steps))
    except Exception as exc:  # noqa: BLE001
        return executor.state.remember(ExecutionResult(record.id, "failed", executor._sanitize(str(exc)), steps))


def _add_plan(
    record: CashCarryPosition,
    item: CashCarryOpportunity,
    settings: BotSettings,
    decision: CashCarryAddDecision,
) -> list[ExecutionStep]:
    return [
        ExecutionStep("transfer_usdt_add", "pending", f"按需划转 USDT，补仓名义 {settings.order_notional_usdt}"),
        ExecutionStep("buy_spot_add", "pending", f"补买现货 {record.symbol}，数量 {item.quantity}"),
        ExecutionStep("set_perp_leverage", "pending", f"确认合约杠杆 {settings.default_leverage}x"),
        ExecutionStep("add_perp_short", "pending", f"基差 {item.basis_pct}% >= {decision.trigger_basis_pct}%，补空合约 {record.symbol}"),
    ]


def _order_decimal(executor, order, fallback: Decimal) -> Decimal:
    return order_average_price(order, fallback)


def _weighted_entry(old_qty: Decimal, old_price: Decimal, add_qty: Decimal, add_price: Decimal) -> Decimal:
    total_qty = old_qty + add_qty
    return (old_qty * old_price + add_qty * add_price) / total_qty if total_qty > 0 else old_price


def _add_fields(
    executor,
    spot_order,
    perp_order,
    quantity: Decimal,
    spot_price: Decimal,
    perp_price: Decimal,
    basis_pct: Decimal,
) -> dict[str, Any]:
    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "quantity": str(quantity),
        "spot_order_id": executor._order_id(spot_order),
        "perp_order_id": executor._order_id(perp_order),
        "spot_price": str(spot_price),
        "perp_price": str(perp_price),
        "basis_pct": str(basis_pct),
    }


def _active_notional(records: list[CashCarryPosition], by_key: dict[tuple[ExchangeName, str], CashCarryOpportunity]) -> Decimal:
    total = Decimal("0")
    for record in records:
        current = by_key.get((record.exchange, record.symbol))
        total += record.quantity * (current.spot_price if current else record.spot_entry_price)
    return total
