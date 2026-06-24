from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR

from app.config import Settings
from app.models import ExecutionPlanStatus, OpenPair, OrderStatus, OrderUpdate, PairDirection, Side


TERMINAL = {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED}


def side_text(side: Side) -> str:
    return "买入" if side == Side.BUY else "卖出"


def status_text(status: OrderStatus) -> str:
    return {
        OrderStatus.NEW: "等待成交",
        OrderStatus.PARTIALLY_FILLED: "部分成交",
        OrderStatus.FILLED: "已成交",
        OrderStatus.CANCELED: "已撤单",
        OrderStatus.REJECTED: "已拒绝",
        OrderStatus.EXPIRED: "已过期",
    }.get(status, status.value)


def lots_from_qty(settings: Settings, qty_oz: Decimal) -> Decimal:
    raw = qty_oz / settings.mt4_lot_size_oz
    stepped = (raw / settings.mt4_lot_step).to_integral_value(rounding=ROUND_FLOOR) * settings.mt4_lot_step
    return max(stepped, settings.mt4_min_lot)


def execution_status(
    settings: Settings,
    active_order: OrderUpdate | None,
    open_pair: OpenPair | None,
    entry_hedge_side: Side | None,
    target_exit_spread,
) -> ExecutionPlanStatus:
    max_follow = Decimal(settings.max_hedge_delay_ms) / Decimal("1000")
    if active_order:
        follow_side = entry_hedge_side
        summary = f"V2 币安限价单：{side_text(active_order.side)} {active_order.orig_qty} XAU，价格 {active_order.price}，状态 {status_text(active_order.status)}。"
        if active_order.reduce_only:
            follow_side = Side.SELL if open_pair and open_pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG else Side.BUY
            summary += " 币安全部成交后 MT4 立刻市价平仓。"
        else:
            summary += " 币安全部成交后 MT4 立刻市价对冲。"
        return ExecutionPlanStatus(
            summary=summary,
            active_binance_order=True,
            binance_order_status=active_order.status,
            binance_order_side=active_order.side,
            binance_order_price=active_order.price,
            binance_order_qty=active_order.orig_qty,
            binance_order_executed_qty=active_order.executed_qty,
            mt4_follow_side=follow_side,
            max_follow_seconds=max_follow,
        )
    if open_pair:
        target = target_exit_spread(open_pair)
        return ExecutionPlanStatus(
            summary=f"V2 组合持仓监控中；不补仓。平仓目标价差 {target} 美元以内，币安只挂 Post Only 平仓，成交后 MT4 跟随。",
            max_follow_seconds=max_follow,
        )
    if settings.gold_v2_observation_only:
        return ExecutionPlanStatus(summary="V2 只读观察中，不会挂单。", max_follow_seconds=max_follow)
    return ExecutionPlanStatus(summary="V2 等待价差达到开仓条件。", max_follow_seconds=max_follow)
