from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from app.config import Settings
from app.models import ExchangeFilters, HistoryBar, MarketQuote, OpenPair, PairDirection, PositionMetrics, Side, utc_now_ms
from app.mt4_costs import live_spread_usd_per_oz, recent_move_budget_usd_per_oz, slippage_budget_usd_per_oz
from app.mt4_rollover import normalize_mt4_rollover_ms
from app.quote_guard import MAX_REASONABLE_XAU_MID_GAP, xau_quote_gap_reason
from app.storage import Storage


LOOKBACK_MS = 30 * 60 * 1000
MIN_POINTS = 8
RANGE_FACTOR = Decimal("0.70")
DEFAULT_SLIPPAGE_BUDGET = Decimal("0.30")
XAU_POINT_VALUE = Decimal("0.01")
MT4_MOVE_PERCENTILE = 70


def build_gold_v2_status(
    settings: Settings,
    storage: Storage,
    filters: ExchangeFilters,
    binance_quote: MarketQuote | None,
    mt4_quote: MarketQuote | None,
    binance_bars: list[HistoryBar],
    open_pair: OpenPair | None,
    metrics: PositionMetrics | None,
    mt4_tick_move_budget: Decimal | None = None,
) -> dict:
    now_ms = utc_now_ms()
    mt4_bars = storage.get_bars("mt4", settings.mt4_symbol, "1m", now_ms - LOOKBACK_MS, now_ms)
    short_range, long_range = _spread_ranges(mt4_bars, binance_bars)
    short_threshold = _entry_threshold(short_range, settings.open_min_edge)
    long_threshold = _entry_threshold(long_range, settings.open_min_edge)
    slippage_budget = _mt4_slippage_budget(settings, mt4_quote, mt4_bars, mt4_tick_move_budget)
    exit_follow_budget = _mt4_exit_follow_budget(settings)
    move_budget_source = "实时tick" if mt4_tick_move_budget is not None else "1分钟K线"

    short_plan = _entry_plan(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        settings=settings,
        filters=filters,
        binance=binance_quote,
        mt4=mt4_quote,
        threshold=short_threshold,
        slippage_budget=slippage_budget,
        exit_follow_budget=exit_follow_budget,
        point_count=short_range["points"],
        spread_range=short_range,
        metrics=metrics,
    )
    long_plan = _entry_plan(
        direction=PairDirection.BINANCE_LONG_MT4_SHORT,
        settings=settings,
        filters=filters,
        binance=binance_quote,
        mt4=mt4_quote,
        threshold=long_threshold,
        slippage_budget=slippage_budget,
        exit_follow_budget=exit_follow_budget,
        point_count=long_range["points"],
        spread_range=long_range,
        metrics=metrics,
    )
    selected = _selected_entry_plan(short_plan, long_plan)

    return {
        "mode": "V2 实盘执行" if not settings.gold_v2_observation_only else "只读观察",
        "auto_trade_enabled": not settings.gold_v2_observation_only,
        "execution_enabled": not settings.gold_v2_observation_only,
        "add_enabled": bool(open_pair and settings.max_add_count > 0),
        "reason": "V2 执行器已解锁，会按币安全限价和 MT4 跟随执行。" if not settings.gold_v2_observation_only else "新版七步方案处于观察阶段，只计算机会和挂单位置，不会自动下单。",
        "lookback_minutes": 30,
        "threshold_rule": "最近30分钟价差最低到最高之间取70%位置，并且不能低于手动最小开仓价差。",
        "mt4_slippage_budget": slippage_budget,
        "mt4_exit_follow_budget": exit_follow_budget,
        "mt4_move_budget_source": move_budget_source,
        "mt4_live_spread_usd_per_oz": live_spread_usd_per_oz(mt4_quote),
        "short_range": short_range,
        "long_range": long_range,
        "short_entry": short_plan,
        "long_entry": long_plan,
        "selected_entry": selected,
        "exit_plan": _exit_plan(settings, open_pair, metrics),
        "add_plan": _add_plan(settings, filters, open_pair, metrics, binance_quote, mt4_quote, slippage_budget, exit_follow_budget),
        "partial_fill_policy": [
            "币安只允许挂单成交，禁止市价开仓和平仓。",
            "补仓按真实首仓价差加阶梯触发，币安仍只允许挂单成交。",
            "部分成交先不让MT4跟随，继续等待原挂单补齐；若交易所终止部分成交单，会自动重挂剩余数量。",
            "MT4跟随失败或超时会先按实盘持仓恢复，仍未完成则自动重发命令，不等待人工处理。",
        ],
    }


def _spread_ranges(mt4_bars: list[HistoryBar], binance_bars: list[HistoryBar]) -> tuple[dict, dict]:
    binance_by_time = {bar.open_time_ms - (bar.open_time_ms % 60_000): bar for bar in binance_bars}
    short_values: list[Decimal] = []
    long_values: list[Decimal] = []
    discarded = 0
    for mt4_bar in mt4_bars:
        aligned = mt4_bar.open_time_ms - (mt4_bar.open_time_ms % 60_000)
        binance_bar = binance_by_time.get(aligned)
        if not binance_bar:
            continue
        diff = binance_bar.close - mt4_bar.close
        if abs(diff) > MAX_REASONABLE_XAU_MID_GAP:
            discarded += 1
            continue
        short_values.append(diff)
        long_values.append(-diff)
    return _range(short_values, discarded), _range(long_values, discarded)


def _range(values: list[Decimal], discarded: int = 0) -> dict:
    if not values:
        return {"points": 0, "discarded": discarded, "low": None, "high": None, "latest": None}
    return {"points": len(values), "discarded": discarded, "low": min(values), "high": max(values), "latest": values[-1]}


def _entry_threshold(spread_range: dict, manual_min: Decimal) -> Decimal:
    if spread_range["points"] < MIN_POINTS or spread_range["low"] is None or spread_range["high"] is None:
        return manual_min
    low = Decimal(str(spread_range["low"]))
    high = Decimal(str(spread_range["high"]))
    dynamic = low + (high - low) * RANGE_FACTOR
    return max(dynamic, manual_min)


def _entry_plan(
    direction: PairDirection,
    settings: Settings,
    filters: ExchangeFilters,
    binance: MarketQuote | None,
    mt4: MarketQuote | None,
    threshold: Decimal,
    slippage_budget: Decimal,
    exit_follow_budget: Decimal,
    point_count: int,
    spread_range: dict,
    metrics: PositionMetrics | None,
) -> dict:
    qty = max(_round_down(settings.target_oz, filters.qty_step), filters.min_qty)
    if not binance or not mt4:
        return _missing_plan(direction, threshold, qty, "等待币安和MT4同时返回报价")
    gap_reason = xau_quote_gap_reason(binance, mt4)
    if gap_reason:
        return _missing_plan(direction, threshold, qty, f"报价异常：{gap_reason}")
    stale_rollover = _stale_rollover_reason(metrics)
    if stale_rollover:
        return _missing_plan(direction, threshold, qty, stale_rollover)
    if direction == PairDirection.BINANCE_SHORT_MT4_LONG:
        current_edge = binance.ask - mt4.ask
        limit_price = _round_up(max(binance.ask + settings.binance_entry_offset_usd, mt4.ask + threshold + slippage_budget), filters.tick_size)
        return _plan_dict(
            direction=direction,
            current_edge=current_edge,
            threshold=threshold,
            qty=qty,
            binance_side=Side.SELL,
            mt4_side=Side.BUY,
            limit_price=limit_price,
            mt4_reference_price=mt4.ask,
            slippage_budget=slippage_budget,
            exit_follow_budget=exit_follow_budget,
            point_count=point_count,
            spread_range=spread_range,
            close_profit=settings.close_profit_usd_per_oz,
            settlement_adjustment=_entry_settlement_adjustment(settings, direction, qty, binance, metrics),
        )
    current_edge = mt4.bid - binance.bid
    limit_price = _round_down(min(binance.bid - settings.binance_entry_offset_usd, mt4.bid - threshold - slippage_budget), filters.tick_size)
    return _plan_dict(
        direction=direction,
        current_edge=current_edge,
        threshold=threshold,
        qty=qty,
        binance_side=Side.BUY,
        mt4_side=Side.SELL,
        limit_price=limit_price,
        mt4_reference_price=mt4.bid,
        slippage_budget=slippage_budget,
        exit_follow_budget=exit_follow_budget,
        point_count=point_count,
        spread_range=spread_range,
        close_profit=settings.close_profit_usd_per_oz,
        settlement_adjustment=_entry_settlement_adjustment(settings, direction, qty, binance, metrics),
    )


def _missing_plan(direction: PairDirection, threshold: Decimal, qty: Decimal, reason: str) -> dict:
    return {
        "direction": direction.value,
        "direction_text": _direction_text(direction),
        "ready": False,
        "reason": reason,
        "current_edge": None,
        "threshold": threshold,
        "quantity_oz": qty,
        "binance_side": None,
        "binance_price": None,
        "mt4_follow_side": None,
    }


def _plan_dict(
    direction: PairDirection,
    current_edge: Decimal,
    threshold: Decimal,
    qty: Decimal,
    binance_side: Side,
    mt4_side: Side,
    limit_price: Decimal,
    mt4_reference_price: Decimal,
    slippage_budget: Decimal,
    exit_follow_budget: Decimal,
    point_count: int,
    spread_range: dict,
    close_profit: Decimal,
    settlement_adjustment: dict | None = None,
) -> dict:
    required_edge = threshold + slippage_budget
    ready = current_edge >= required_edge
    locked_edge = limit_price - mt4_reference_price if binance_side == Side.SELL else mt4_reference_price - limit_price
    adjustment_value = Decimal("0")
    if settlement_adjustment:
        adjustment_value = Decimal(str(settlement_adjustment["total"]))
    estimated_exit_target = max(Decimal("0"), locked_edge + (adjustment_value / qty) - slippage_budget - exit_follow_budget - close_profit)
    recent_low = Decimal(str(spread_range["low"])) if spread_range.get("low") is not None else None
    exit_viable = recent_low is None or recent_low <= estimated_exit_target
    reason = "达到安全入场边际，可以进入小仓验证队列" if ready else f"当前价差未到安全入场边际 {required_edge}"
    if ready and not exit_viable:
        ready = False
        reason = f"达到入场阈值，但最近30分钟最低价差 {recent_low} > 扣除下次资金费和隔夜费后的安全平仓价差 {estimated_exit_target}，暂不开仓"
    if point_count < MIN_POINTS:
        reason += "，30分钟样本不足，暂用手动阈值"
    return {
        "direction": direction.value,
        "direction_text": _direction_text(direction),
        "ready": ready,
        "reason": reason,
        "current_edge": current_edge,
        "threshold": threshold,
        "required_edge": required_edge,
        "quantity_oz": qty,
        "binance_side": binance_side.value,
        "binance_price": limit_price,
        "mt4_follow_side": mt4_side.value,
        "expected_locked_edge": locked_edge,
        "estimated_exit_target_spread": estimated_exit_target,
        "recent_low_spread": recent_low,
        "exit_viable": exit_viable,
        "next_settlement_adjustment": settlement_adjustment,
        "mt4_reference_price": mt4_reference_price,
        "mt4_slippage_budget": slippage_budget,
        "mt4_exit_follow_budget": exit_follow_budget,
    }


def _entry_settlement_adjustment(
    settings: Settings,
    direction: PairDirection,
    qty: Decimal,
    binance: MarketQuote | None,
    metrics: PositionMetrics | None,
) -> dict | None:
    if not metrics or qty <= 0:
        return None
    funding = _entry_binance_funding_estimate(direction, qty, binance, metrics)
    swap = _entry_mt4_swap_estimate(settings, direction, qty, metrics)
    if funding is None and swap is None:
        return None
    funding = funding or Decimal("0")
    swap = swap or Decimal("0")
    return {
        "binance_funding": funding,
        "mt4_swap": swap,
        "total": funding + swap,
    }


def _stale_rollover_reason(metrics: PositionMetrics | None) -> str | None:
    if not metrics or metrics.mt4_next_rollover_time_ms is None:
        return None
    if metrics.mt4_next_rollover_time_ms <= utc_now_ms() and normalize_mt4_rollover_ms(metrics.mt4_next_rollover_time_ms) is None:
        return "MT4 隔夜费结算时间已过期，等待 EA 刷新下一次结算时间后再评估开仓"
    return None


def _entry_binance_funding_estimate(
    direction: PairDirection,
    qty: Decimal,
    binance: MarketQuote | None,
    metrics: PositionMetrics,
) -> Decimal | None:
    if not binance or metrics.binance_funding_rate is None:
        return None
    amount = binance.mid * qty * metrics.binance_funding_rate
    if direction == PairDirection.BINANCE_LONG_MT4_SHORT:
        amount = -amount
    return amount


def _entry_mt4_swap_estimate(
    settings: Settings,
    direction: PairDirection,
    qty: Decimal,
    metrics: PositionMetrics,
) -> Decimal | None:
    raw = metrics.mt4_swap_long_per_lot if direction == PairDirection.BINANCE_SHORT_MT4_LONG else metrics.mt4_swap_short_per_lot
    if raw is None:
        return None
    next_rollover_time_ms = normalize_mt4_rollover_ms(metrics.mt4_next_rollover_time_ms)
    if metrics.mt4_next_rollover_time_ms is not None and next_rollover_time_ms is None and metrics.mt4_next_rollover_time_ms <= utc_now_ms():
        return None
    lots = qty / settings.mt4_lot_size_oz
    multiplier = _entry_mt4_swap_multiplier(settings, next_rollover_time_ms)
    return raw * lots * multiplier


def _entry_mt4_swap_multiplier(settings: Settings, next_rollover_time_ms: int | None) -> Decimal:
    if next_rollover_time_ms is None:
        return Decimal("1")
    rollover_day = datetime.fromtimestamp(next_rollover_time_ms / 1000, timezone.utc).weekday()
    if rollover_day == settings.mt4_triple_swap_weekday:
        return settings.mt4_triple_swap_multiplier
    return Decimal("1")


def _selected_entry_plan(short_plan: dict, long_plan: dict) -> dict | None:
    ready = [plan for plan in (short_plan, long_plan) if plan.get("ready")]
    if not ready:
        return max((short_plan, long_plan), key=lambda plan: Decimal(str(plan.get("current_edge") or "-999999")))
    return max(ready, key=lambda plan: Decimal(str(plan.get("current_edge") or "0")) - Decimal(str(plan.get("threshold") or "0")))


def _exit_plan(settings: Settings, pair: OpenPair | None, metrics: PositionMetrics | None) -> dict:
    if not pair:
        return {"enabled": False, "reason": "当前无组合持仓，不计算平仓价差。"}
    if not metrics or metrics.dynamic_close_spread is None:
        return {"enabled": False, "reason": "等待真实均价、资金费和隔夜费数据后再计算平仓价差。"}
    loss_limit = _loss_limit_exit_plan(settings, pair, metrics)
    negative_swap = _negative_swap_exit_plan(settings, pair, metrics)
    target_exit_spread = (
        loss_limit["target_exit_spread"]
        if loss_limit.get("active") and loss_limit.get("target_exit_spread") is not None
        else
        negative_swap["target_exit_spread"]
        if negative_swap.get("active") and negative_swap.get("target_exit_spread") is not None
        else metrics.dynamic_close_spread
    )
    reason = "平仓目标来自真实均价、已产生费用和额外利润空间。"
    if loss_limit.get("active"):
        reason = loss_limit["reason"]
    elif negative_swap.get("active"):
        reason = negative_swap["reason"]
    return {
        "enabled": True,
        "direction": pair.direction.value,
        "current_exit_spread": metrics.current_exit_spread,
        "break_even_spread": metrics.profitable_spread_threshold,
        "target_exit_spread": target_exit_spread,
        "estimated_net": metrics.estimated_close_net,
        "reason": reason,
        "normal_target_exit_spread": metrics.dynamic_close_spread,
        "loss_limit": loss_limit,
        "negative_swap": negative_swap,
    }


def _loss_limit_exit_plan(settings: Settings, pair: OpenPair, metrics: PositionMetrics) -> dict:
    if settings.max_pair_loss_usdt <= 0 or pair.quantity_oz <= 0:
        return {"active": False, "reason": "组合最大亏损风控未启用。"}
    if metrics.estimated_close_net is None or metrics.profitable_spread_threshold is None:
        return {"active": False, "reason": "等待组合净值和保本价差后再判断最大亏损。"}
    max_loss = settings.max_pair_loss_usdt
    if metrics.estimated_close_net > -max_loss:
        return {
            "active": False,
            "reason": f"当前预估净值 {metrics.estimated_close_net}，未触发最大亏损 {max_loss}。",
            "max_loss_usdt": max_loss,
        }
    target = max(Decimal("0"), metrics.profitable_spread_threshold + (max_loss / pair.quantity_oz))
    return {
        "active": True,
        "reason": f"组合预估净值 {metrics.estimated_close_net} 已触发最大亏损 {max_loss}，只用币安限价尝试在可控亏损内离场。",
        "target_exit_spread": target,
        "estimated_net": metrics.estimated_close_net,
        "max_loss_usdt": max_loss,
    }


def _negative_swap_exit_plan(settings: Settings, pair: OpenPair, metrics: PositionMetrics) -> dict:
    estimate = metrics.mt4_swap_estimate
    next_rollover = metrics.mt4_next_rollover_time_ms
    if settings.negative_swap_close_before_minutes <= 0 or estimate is None or estimate >= 0 or next_rollover is None:
        return {"active": False, "reason": "隔夜费风控未触发。"}
    ms_left = next_rollover - utc_now_ms()
    lead_ms = settings.negative_swap_close_before_minutes * 60 * 1000
    projected_net = _projected_convergence_net_after_next_swap(settings, pair, metrics, estimate)
    minutes_left = max(0, ms_left // 60_000)
    if ms_left < 0:
        return {
            "active": False,
            "reason": f"MT4 下次隔夜费预估亏损 {estimate}，结算点刚过，等待 MT4 刷新下一次隔夜费时间。",
            "projected_convergence_net": projected_net,
            "minutes_left": 0,
        }
    if ms_left > lead_ms:
        return {
            "active": False,
            "reason": f"MT4 下次隔夜费预估亏损 {estimate}，距离结算约 {minutes_left} 分钟，未到提前处理窗口。",
            "projected_convergence_net": projected_net,
            "minutes_left": minutes_left,
        }
    if projected_net is not None and projected_net > 0:
        return {
            "active": False,
            "reason": f"MT4 下次隔夜费预估亏损 {estimate}，但扣后回归净利预估 {projected_net}，不提前平仓。",
            "projected_convergence_net": projected_net,
            "minutes_left": minutes_left,
        }
    safe_target = None
    if metrics.profitable_spread_threshold is not None:
        safe_target = max(
            Decimal("0"),
            metrics.profitable_spread_threshold - (metrics.exit_follow_buffer_usd_per_oz or Decimal("0")),
        )
    return {
        "active": safe_target is not None,
        "reason": f"隔夜费亏损风控进入处理窗口：预估 {estimate}，扣后回归净利 {projected_net}，距离结算约 {minutes_left} 分钟；只在不亏保护价差内平仓。",
        "target_exit_spread": safe_target,
        "projected_convergence_net": projected_net,
        "minutes_left": minutes_left,
    }


def _projected_convergence_net_after_next_swap(
    settings: Settings,
    pair: OpenPair,
    metrics: PositionMetrics,
    next_swap: Decimal,
) -> Decimal | None:
    if metrics.actual_entry_spread is None:
        return None
    net = (metrics.actual_entry_spread - settings.close_max_spread) * pair.quantity_oz
    net -= metrics.estimated_fees or Decimal("0")
    net += metrics.binance_accrued_funding or Decimal("0")
    net += metrics.mt4_accrued_swap or Decimal("0")
    return net + next_swap


def _add_plan(
    settings: Settings,
    filters: ExchangeFilters,
    pair: OpenPair | None,
    metrics: PositionMetrics | None,
    binance: MarketQuote | None,
    mt4: MarketQuote | None,
    slippage_budget: Decimal,
    exit_follow_budget: Decimal,
) -> dict:
    if not pair:
        return {"enabled": False, "reason": "无持仓，补仓计划不启动。"}
    if settings.max_add_count <= 0:
        return {"enabled": False, "reason": "补仓次数上限为0，补仓关闭。"}
    if pair.add_count >= settings.max_add_count:
        return {"enabled": False, "reason": "补仓次数已达上限。", "add_count": pair.add_count, "max_add_count": settings.max_add_count}
    base = _add_base_edge(pair, metrics)
    if base is None:
        return {"enabled": False, "reason": "等待组合真实均价差后再计算补仓阶梯。"}
    next_count = pair.add_count + 1
    trigger = base + settings.add_edge_growth_usd * Decimal(next_count)
    data = {
        "enabled": True,
        "reason": "等待价差走扩到补仓触发位。",
        "base_edge": base,
        "next_add_number": next_count,
        "next_trigger_edge": trigger,
        "add_count": pair.add_count,
        "max_add_count": settings.max_add_count,
        "quantity_oz": max(_round_down(settings.target_oz, filters.qty_step), filters.min_qty),
    }
    if not binance or not mt4:
        return {**data, "ready": False, "reason": "等待币安和MT4同时返回报价。"}
    qty = data["quantity_oz"]
    if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
        edge = binance.ask - mt4.ask
        price = _round_up(max(binance.ask + settings.binance_entry_offset_usd, mt4.ask + trigger + slippage_budget), filters.tick_size)
        side, mt4_side, locked = Side.SELL, Side.BUY, price - mt4.ask
    else:
        edge = mt4.bid - binance.bid
        price = _round_down(min(binance.bid - settings.binance_entry_offset_usd, mt4.bid - trigger - slippage_budget), filters.tick_size)
        side, mt4_side, locked = Side.BUY, Side.SELL, mt4.bid - price
    current_avg_edge = metrics.actual_entry_spread if metrics and metrics.actual_entry_spread is not None else pair.base_edge
    blended_edge = None
    exit_viable = False
    if current_avg_edge is not None:
        blended_edge = ((current_avg_edge * pair.quantity_oz) + (locked * qty)) / (pair.quantity_oz + qty)
        exit_viable = blended_edge > settings.close_profit_usd_per_oz + exit_follow_budget
    ready = edge >= trigger and exit_viable
    reason = "达到补仓触发位，可以挂补仓限价单。"
    if not exit_viable:
        reason = "补仓后均价差仍不足以覆盖平仓缓冲和目标利润，暂不补仓。"
    elif not ready:
        reason = "当前价差未到补仓触发位。"
    return {**data,
        "ready": ready,
        "reason": reason,
        "current_edge": edge,
        "binance_side": side.value,
        "binance_price": price,
        "mt4_follow_side": mt4_side.value,
        "expected_locked_edge": locked,
        "estimated_blended_edge": blended_edge,
        "exit_viable": exit_viable,
        "mt4_slippage_budget": slippage_budget,
        "mt4_exit_follow_budget": exit_follow_budget,
    }


def _add_base_edge(pair: OpenPair, metrics: PositionMetrics | None) -> Decimal | None:
    if pair.add_count == 0 and metrics and metrics.actual_entry_spread is not None:
        return metrics.actual_entry_spread
    return pair.base_edge or (metrics.actual_entry_spread if metrics else None)


def _mt4_slippage_budget(
    settings: Settings,
    mt4_quote: MarketQuote | None = None,
    mt4_bars: list[HistoryBar] | None = None,
    tick_move_budget: Decimal | None = None,
) -> Decimal:
    configured = slippage_budget_usd_per_oz(settings.mt4_slippage_points, XAU_POINT_VALUE, mt4_quote)
    base = max(configured, DEFAULT_SLIPPAGE_BUDGET)
    recent = tick_move_budget if tick_move_budget is not None else _mt4_recent_move_budget(mt4_bars or [])
    return base + recent


def _mt4_exit_follow_budget(settings: Settings) -> Decimal:
    return Decimal(settings.mt4_slippage_points) * XAU_POINT_VALUE + settings.mt4_close_extra_buffer_usd


def _mt4_recent_move_budget(mt4_bars: list[HistoryBar]) -> Decimal:
    return recent_move_budget_usd_per_oz(mt4_bars, percentile=MT4_MOVE_PERCENTILE, min_points=MIN_POINTS)


def _direction_text(direction: PairDirection) -> str:
    if direction == PairDirection.BINANCE_SHORT_MT4_LONG:
        return "币安做空，MT4做多"
    return "币安做多，MT4做空"


def _round_down(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _round_up(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step
