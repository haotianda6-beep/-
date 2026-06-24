from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from app.config import Settings
from app.models import ExchangeFilters, HistoryBar, MarketQuote, OpenPair, PairDirection, PositionMetrics, Side, utc_now_ms
from app.storage import Storage


LOOKBACK_MS = 30 * 60 * 1000
MIN_POINTS = 8
RANGE_FACTOR = Decimal("0.70")
DEFAULT_SLIPPAGE_BUDGET = Decimal("0.30")
XAU_POINT_VALUE = Decimal("0.01")


def build_gold_v2_status(
    settings: Settings,
    storage: Storage,
    filters: ExchangeFilters,
    binance_quote: MarketQuote | None,
    mt4_quote: MarketQuote | None,
    binance_bars: list[HistoryBar],
    open_pair: OpenPair | None,
    metrics: PositionMetrics | None,
) -> dict:
    now_ms = utc_now_ms()
    mt4_bars = storage.get_bars("mt4", settings.mt4_symbol, "1m", now_ms - LOOKBACK_MS, now_ms)
    short_range, long_range = _spread_ranges(mt4_bars, binance_bars)
    short_threshold = _entry_threshold(short_range, settings.open_min_edge)
    long_threshold = _entry_threshold(long_range, settings.open_min_edge)
    slippage_budget = _mt4_slippage_budget(settings)

    short_plan = _entry_plan(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        settings=settings,
        filters=filters,
        binance=binance_quote,
        mt4=mt4_quote,
        threshold=short_threshold,
        slippage_budget=slippage_budget,
        point_count=short_range["points"],
    )
    long_plan = _entry_plan(
        direction=PairDirection.BINANCE_LONG_MT4_SHORT,
        settings=settings,
        filters=filters,
        binance=binance_quote,
        mt4=mt4_quote,
        threshold=long_threshold,
        slippage_budget=slippage_budget,
        point_count=long_range["points"],
    )
    selected = _selected_entry_plan(short_plan, long_plan)

    return {
        "mode": "V2 实盘执行" if not settings.gold_v2_observation_only else "只读观察",
        "auto_trade_enabled": not settings.gold_v2_observation_only,
        "execution_enabled": not settings.gold_v2_observation_only,
        "add_enabled": False,
        "reason": "V2 执行器已解锁，会按币安全限价和 MT4 跟随执行。" if not settings.gold_v2_observation_only else "新版七步方案处于观察阶段，只计算机会和挂单位置，不会自动下单。",
        "lookback_minutes": 30,
        "threshold_rule": "最近30分钟价差最低到最高之间取70%位置，并且不能低于手动最小开仓价差。",
        "mt4_slippage_budget": slippage_budget,
        "short_range": short_range,
        "long_range": long_range,
        "short_entry": short_plan,
        "long_entry": long_plan,
        "selected_entry": selected,
        "exit_plan": _exit_plan(open_pair, metrics),
        "add_plan": _disabled_add_plan(settings, open_pair, metrics),
        "partial_fill_policy": [
            "币安只允许挂单成交，禁止市价开仓和平仓。",
            "首版不启用补仓；补仓触发价会计算但不会执行。",
            "部分成交先不让MT4跟随，继续等待原挂单补齐到目标数量；超时后暂停并提示人工确认。",
        ],
    }


def _spread_ranges(mt4_bars: list[HistoryBar], binance_bars: list[HistoryBar]) -> tuple[dict, dict]:
    binance_by_time = {bar.open_time_ms - (bar.open_time_ms % 60_000): bar for bar in binance_bars}
    short_values: list[Decimal] = []
    long_values: list[Decimal] = []
    for mt4_bar in mt4_bars:
        aligned = mt4_bar.open_time_ms - (mt4_bar.open_time_ms % 60_000)
        binance_bar = binance_by_time.get(aligned)
        if not binance_bar:
            continue
        diff = binance_bar.close - mt4_bar.close
        short_values.append(diff)
        long_values.append(-diff)
    return _range(short_values), _range(long_values)


def _range(values: list[Decimal]) -> dict:
    if not values:
        return {"points": 0, "low": None, "high": None, "latest": None}
    return {"points": len(values), "low": min(values), "high": max(values), "latest": values[-1]}


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
    point_count: int,
) -> dict:
    qty = max(_round_down(settings.target_oz, filters.qty_step), filters.min_qty)
    if not binance or not mt4:
        return _missing_plan(direction, threshold, qty, "等待币安和MT4同时返回报价")
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
            point_count=point_count,
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
        point_count=point_count,
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
    point_count: int,
) -> dict:
    ready = current_edge >= threshold
    locked_edge = limit_price - mt4_reference_price if binance_side == Side.SELL else mt4_reference_price - limit_price
    reason = "达到观察阈值，可以进入小仓验证队列" if ready else "当前价差未到观察阈值"
    if point_count < MIN_POINTS:
        reason += "，30分钟样本不足，暂用手动阈值"
    return {
        "direction": direction.value,
        "direction_text": _direction_text(direction),
        "ready": ready,
        "reason": reason,
        "current_edge": current_edge,
        "threshold": threshold,
        "quantity_oz": qty,
        "binance_side": binance_side.value,
        "binance_price": limit_price,
        "mt4_follow_side": mt4_side.value,
        "expected_locked_edge": locked_edge,
        "mt4_reference_price": mt4_reference_price,
        "mt4_slippage_budget": slippage_budget,
    }


def _selected_entry_plan(short_plan: dict, long_plan: dict) -> dict | None:
    ready = [plan for plan in (short_plan, long_plan) if plan.get("ready")]
    if not ready:
        return max((short_plan, long_plan), key=lambda plan: Decimal(str(plan.get("current_edge") or "-999999")))
    return max(ready, key=lambda plan: Decimal(str(plan.get("current_edge") or "0")) - Decimal(str(plan.get("threshold") or "0")))


def _exit_plan(pair: OpenPair | None, metrics: PositionMetrics | None) -> dict:
    if not pair:
        return {"enabled": False, "reason": "当前无组合持仓，不计算平仓价差。"}
    if not metrics or metrics.dynamic_close_spread is None:
        return {"enabled": False, "reason": "等待真实均价、资金费和隔夜费数据后再计算平仓价差。"}
    return {
        "enabled": True,
        "direction": pair.direction.value,
        "current_exit_spread": metrics.current_exit_spread,
        "break_even_spread": metrics.profitable_spread_threshold,
        "target_exit_spread": metrics.dynamic_close_spread,
        "estimated_net": metrics.estimated_close_net,
        "reason": "平仓目标来自真实均价、已产生费用和额外利润空间。",
    }


def _disabled_add_plan(settings: Settings, pair: OpenPair | None, metrics: PositionMetrics | None) -> dict:
    if not pair:
        return {"enabled": False, "reason": "无持仓，补仓计划不启动。"}
    base = metrics.actual_entry_spread if metrics and metrics.actual_entry_spread is not None else pair.base_edge
    if base is None:
        return {"enabled": False, "reason": "等待组合真实均价差后再计算补仓阶梯。"}
    next_count = pair.add_count + 1
    trigger = base + settings.add_edge_growth_usd * Decimal(next_count)
    return {
        "enabled": False,
        "reason": "补仓代码已计算，但七步观察阶段不执行。",
        "base_edge": base,
        "next_add_number": next_count,
        "next_trigger_edge": trigger,
        "max_add_count": settings.max_add_count,
    }


def _mt4_slippage_budget(settings: Settings) -> Decimal:
    configured = Decimal(settings.mt4_slippage_points) * XAU_POINT_VALUE
    return max(configured, DEFAULT_SLIPPAGE_BUDGET)


def _direction_text(direction: PairDirection) -> str:
    if direction == PairDirection.BINANCE_SHORT_MT4_LONG:
        return "币安做空，MT4做多"
    return "币安做多，MT4做空"


def _round_down(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _round_up(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step
