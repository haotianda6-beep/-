from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from app.config import Settings
from app.execution_slippage import mt4_close_slippage_budget_usd_per_oz, mt4_entry_slippage_budget_usd_per_oz
from app.market_calendar import is_xau_weekend_ms
from app.models import ExchangeFilters, HistoryBar, MarketQuote, OpenPair, PairDirection, PositionMetrics, Side, utc_now_ms
from app.mt4_costs import live_spread_usd_per_oz, recent_move_budget_usd_per_oz, slippage_budget_usd_per_oz
from app.mt4_rollover import normalize_mt4_rollover_ms
from app.quote_guard import MAX_REASONABLE_XAU_MID_GAP, xau_quote_gap_reason
from app.storage import Storage
from app.v2_tuning import build_entry_model


RANGE_LOOKBACK_MS = 30 * 60 * 1000
MODEL_LOOKBACK_MS = 48 * 60 * 60 * 1000
MIN_POINTS = 8
RANGE_FACTOR = Decimal("0.70")
DEFAULT_SLIPPAGE_BUDGET = Decimal("0.30")
XAU_POINT_VALUE = Decimal("0.01")
MT4_MOVE_PERCENTILE = 70
MIN_ADD_EDGE_IMPROVEMENT = Decimal("0.20")
ENTRY_AGED_PROFIT_WINDOW_MINUTES = 120
MAX_TRAINING_MT4_BAR_RANGE = Decimal("4")
MAX_TRAINING_BINANCE_BAR_RANGE = Decimal("5")
MAX_TRADABLE_ABS_SPREAD = MAX_REASONABLE_XAU_MID_GAP
MAX_TRAINING_ABS_SPREAD = MAX_TRADABLE_ABS_SPREAD
PERFORMANCE_LOOKBACK_MS = 7 * 24 * 60 * 60 * 1000
PERFORMANCE_MIN_TRADES = 3
PERFORMANCE_TARGET_WIN_RATE = Decimal("0.70")
PERFORMANCE_MIN_PROJECTED_DAILY_TRADES = Decimal("3")
MAX_PERFORMANCE_ENTRY_PENALTY = Decimal("0.50")


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
    realized_performance: dict | None = None,
) -> dict:
    now_ms = utc_now_ms()
    mt4_bars = storage.get_bars("mt4", settings.mt4_symbol, "1m", now_ms - RANGE_LOOKBACK_MS, now_ms)
    mt4_model_bars = storage.get_bars("mt4", settings.mt4_symbol, "1m", now_ms - MODEL_LOOKBACK_MS, now_ms)
    range_binance_bars = [bar for bar in binance_bars if bar.open_time_ms >= now_ms - RANGE_LOOKBACK_MS]
    short_values, long_values, discarded = _spread_values(mt4_bars, range_binance_bars)
    model_short_values, model_long_values, _ = _spread_values(mt4_model_bars, binance_bars)
    short_range, long_range = _range(short_values, discarded), _range(long_values, discarded)
    slippage_budget = _mt4_slippage_budget(settings, storage, now_ms, mt4_quote, mt4_bars, mt4_tick_move_budget)
    exit_follow_budget = _mt4_exit_follow_budget(settings, storage, now_ms)
    entry_close_profit = _entry_viability_close_profit(settings)
    short_model = build_entry_model(
        values=model_short_values,
        manual_min=settings.open_min_edge,
        slippage_budget=slippage_budget,
        exit_follow_budget=exit_follow_budget,
        close_profit=settings.close_profit_usd_per_oz,
        max_hold_minutes=settings.max_pair_age_minutes,
        min_points=MIN_POINTS,
        entry_cooldown_minutes=_entry_cooldown_minutes(settings),
        spread_protection_budget=_model_spread_protection_budget(mt4_quote),
        aged_close_profit=entry_close_profit,
        max_threshold=MAX_TRADABLE_ABS_SPREAD,
    )
    long_model = build_entry_model(
        values=model_long_values,
        manual_min=settings.open_min_edge,
        slippage_budget=slippage_budget,
        exit_follow_budget=exit_follow_budget,
        close_profit=settings.close_profit_usd_per_oz,
        max_hold_minutes=settings.max_pair_age_minutes,
        min_points=MIN_POINTS,
        entry_cooldown_minutes=_entry_cooldown_minutes(settings),
        spread_protection_budget=_model_spread_protection_budget(mt4_quote),
        aged_close_profit=entry_close_profit,
        max_threshold=MAX_TRADABLE_ABS_SPREAD,
    )
    short_threshold = _entry_threshold(short_range, settings.open_min_edge, short_model)
    long_threshold = _entry_threshold(long_range, settings.open_min_edge, long_model)
    realized_performance = realized_performance or _v2_realized_performance(storage, now_ms)
    adjustment_performance, adjustment_scope = _performance_for_entry_adjustment(realized_performance)
    performance_penalty = _performance_entry_penalty(adjustment_performance)
    short_performance_penalty = _directional_performance_entry_penalty(adjustment_performance, "short", performance_penalty)
    long_performance_penalty = _directional_performance_entry_penalty(adjustment_performance, "long", performance_penalty)
    short_performance_cap = _performance_threshold_cap(short_model)
    long_performance_cap = _performance_threshold_cap(long_model)
    short_threshold = _threshold_with_performance_penalty(short_threshold, short_performance_penalty, short_model)
    long_threshold = _threshold_with_performance_penalty(long_threshold, long_performance_penalty, long_model)
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
        entry_close_profit=entry_close_profit,
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
        entry_close_profit=entry_close_profit,
    )
    selected = _selected_entry_plan(short_plan, long_plan)
    objective_health = _objective_health(
        realized_performance=realized_performance,
        short_model=short_model,
        long_model=long_model,
        short_threshold=short_threshold,
        long_threshold=long_threshold,
    )

    return {
        "mode": "V2 实盘执行" if not settings.gold_v2_observation_only else "只读观察",
        "auto_trade_enabled": not settings.gold_v2_observation_only,
        "execution_enabled": not settings.gold_v2_observation_only,
        "add_enabled": bool(open_pair and settings.max_add_count > 0),
        "reason": "V2 执行器已解锁，会按币安全限价和 MT4 跟随执行。" if not settings.gold_v2_observation_only else "新版七步方案处于观察阶段，只计算机会和挂单位置，不会自动下单。",
        "lookback_minutes": 30,
        "entry_model_lookback_minutes": 2880,
        "threshold_rule": "先剔除MT4停盘平线、周末和剧烈跳价样本；可信长周期模型负责胜率，只有模型阈值超过黄金正常上限时才回退到最近30分钟区间。",
        "entry_model": {"short": short_model, "long": long_model},
        "objective_health": objective_health,
        "realized_performance": realized_performance,
        "performance_entry_penalty": performance_penalty,
        "performance_adjustment_scope": adjustment_scope,
        "performance_adjustment_sample_count": int(adjustment_performance.get("sample_count") or 0),
        "directional_performance_entry_penalty": {
            "short": short_performance_penalty,
            "long": long_performance_penalty,
        },
        "performance_threshold_cap": {"short": short_performance_cap, "long": long_performance_cap},
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


def _spread_values(mt4_bars: list[HistoryBar], binance_bars: list[HistoryBar]) -> tuple[list[Decimal], list[Decimal], int]:
    binance_by_time = {bar.open_time_ms - (bar.open_time_ms % 60_000): bar for bar in binance_bars}
    short_values: list[Decimal] = []
    long_values: list[Decimal] = []
    discarded = 0
    previous_mt4_close: Decimal | None = None
    for mt4_bar in mt4_bars:
        aligned = mt4_bar.open_time_ms - (mt4_bar.open_time_ms % 60_000)
        binance_bar = binance_by_time.get(aligned)
        if not binance_bar:
            previous_mt4_close = mt4_bar.close
            continue
        diff = binance_bar.close - mt4_bar.close
        if _untradable_training_bar(mt4_bar, binance_bar, diff, previous_mt4_close):
            discarded += 1
            previous_mt4_close = mt4_bar.close
            continue
        short_values.append(diff)
        long_values.append(-diff)
        previous_mt4_close = mt4_bar.close
    return short_values, long_values, discarded


def _untradable_training_bar(
    mt4_bar: HistoryBar,
    binance_bar: HistoryBar,
    diff: Decimal,
    previous_mt4_close: Decimal | None = None,
) -> bool:
    if _weekend_bar(mt4_bar.open_time_ms):
        return True
    if abs(diff) > min(MAX_REASONABLE_XAU_MID_GAP, MAX_TRAINING_ABS_SPREAD):
        return True
    if mt4_bar.high <= mt4_bar.low and previous_mt4_close == mt4_bar.close:
        return True
    if mt4_bar.high > mt4_bar.low and mt4_bar.high - mt4_bar.low > MAX_TRAINING_MT4_BAR_RANGE:
        return True
    if binance_bar.high > binance_bar.low and binance_bar.high - binance_bar.low > MAX_TRAINING_BINANCE_BAR_RANGE:
        return True
    return False


def _weekend_bar(open_time_ms: int) -> bool:
    return is_xau_weekend_ms(open_time_ms)


def _range(values: list[Decimal], discarded: int = 0) -> dict:
    if not values:
        return {"points": 0, "discarded": discarded, "low": None, "high": None, "latest": None}
    return {"points": len(values), "discarded": discarded, "low": min(values), "high": max(values), "latest": values[-1]}


def _entry_threshold(spread_range: dict, manual_min: Decimal, model: dict | None = None) -> Decimal:
    fallback = _range_threshold(spread_range, manual_min)
    if model and model.get("suggested_threshold") is not None:
        model_threshold = max(manual_min, Decimal(str(model["suggested_threshold"])))
        if model_threshold > MAX_TRADABLE_ABS_SPREAD:
            return fallback
        return model_threshold
    return fallback


def _threshold_with_performance_penalty(threshold: Decimal, penalty: Decimal, model: dict | None = None) -> Decimal:
    if penalty <= 0:
        return threshold
    capped = min(MAX_TRADABLE_ABS_SPREAD, threshold + penalty)
    performance_cap = _performance_threshold_cap(model)
    if performance_cap is None:
        return capped
    return min(capped, max(threshold, performance_cap))


def _performance_threshold_cap(model: dict | None) -> Decimal | None:
    if not model:
        return None
    candidates = model.get("candidates") or []
    eligible = []
    for candidate in candidates:
        try:
            threshold = Decimal(str(candidate.get("threshold")))
            projected = Decimal(str(candidate.get("projected_daily_trades") or "0"))
        except Exception:  # noqa: BLE001
            continue
        if projected >= PERFORMANCE_MIN_PROJECTED_DAILY_TRADES and threshold <= MAX_TRADABLE_ABS_SPREAD:
            eligible.append(threshold)
    return max(eligible) if eligible else None


def _range_threshold(spread_range: dict, manual_min: Decimal) -> Decimal:
    if spread_range["points"] < MIN_POINTS or spread_range["low"] is None or spread_range["high"] is None:
        return manual_min
    low = Decimal(str(spread_range["low"]))
    high = Decimal(str(spread_range["high"]))
    dynamic = low + (high - low) * RANGE_FACTOR
    return max(dynamic, manual_min)


def _v2_realized_performance(storage: Storage, now_ms: int) -> dict:
    try:
        events = storage.get_events(now_ms - PERFORMANCE_LOOKBACK_MS, now_ms + 1000, limit=2000)
    except Exception:  # noqa: BLE001
        return {
            "sample_count": 0,
            "reason": "真实做单统计暂不可用，不参与阈值调整。",
        }
    pnls: list[Decimal] = []
    for event in events:
        if event.get("kind") != "v2_pair_pnl_recorded":
            continue
        payload = event.get("payload") or {}
        try:
            pnls.append(Decimal(str(payload.get("realized_pnl"))))
        except Exception:  # noqa: BLE001
            continue
    if not pnls:
        return {
            "sample_count": 0,
            "reason": "暂无 V2 真实闭环样本，不参与阈值调整。",
        }
    wins = sum(1 for pnl in pnls if pnl > 0)
    losses = sum(1 for pnl in pnls if pnl < 0)
    total = sum(pnls, Decimal("0"))
    win_rate = Decimal(wins) / Decimal(len(pnls))
    return {
        "sample_count": len(pnls),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "target_win_rate": PERFORMANCE_TARGET_WIN_RATE,
        "total_pnl": total,
        "average_pnl": total / Decimal(len(pnls)),
        "latest_pnl": pnls[-1],
        "min_pnl": min(pnls),
        "max_pnl": max(pnls),
        "reason": _performance_reason(len(pnls), win_rate, total),
    }


def _performance_reason(sample_count: int, win_rate: Decimal, total_pnl: Decimal) -> str:
    if sample_count < PERFORMANCE_MIN_TRADES:
        return f"V2 真实闭环样本 {sample_count} 单，少于 {PERFORMANCE_MIN_TRADES} 单，暂不硬性调整阈值。"
    if win_rate >= PERFORMANCE_TARGET_WIN_RATE:
        return f"V2 真实胜率 {win_rate:.2%} 达到目标，不额外抬高开仓阈值。"
    if total_pnl >= 0:
        return f"V2 真实胜率 {win_rate:.2%} 未达目标，但总收益仍为正，只做小幅保护。"
    return f"V2 真实胜率 {win_rate:.2%} 未达 {PERFORMANCE_TARGET_WIN_RATE:.0%} 且总收益为负，自动抬高开仓阈值保护本金。"


def _performance_entry_penalty(performance: dict) -> Decimal:
    sample_count = int(performance.get("sample_count") or 0)
    if sample_count < PERFORMANCE_MIN_TRADES:
        return Decimal("0")
    win_rate = Decimal(str(performance.get("win_rate") or "0"))
    if win_rate >= PERFORMANCE_TARGET_WIN_RATE:
        return Decimal("0")
    gap = PERFORMANCE_TARGET_WIN_RATE - win_rate
    penalty = min(MAX_PERFORMANCE_ENTRY_PENALTY, max(Decimal("0"), gap))
    total = Decimal(str(performance.get("total_pnl") or "0"))
    if total >= 0:
        penalty = min(penalty, Decimal("0.20"))
    return penalty


def _performance_for_entry_adjustment(performance: dict) -> tuple[dict, str]:
    current_guard = performance.get("current_guard")
    if isinstance(current_guard, dict):
        sample_count = int(current_guard.get("sample_count") or 0)
        if sample_count < PERFORMANCE_MIN_TRADES:
            return current_guard, "current_guard_bootstrap"
        return current_guard, "current_guard"
    return performance, "overall"


def _directional_performance_entry_penalty(performance: dict, direction: str, fallback: Decimal) -> Decimal:
    directions = performance.get("directions")
    if not isinstance(directions, dict):
        return fallback
    direction_performance = directions.get(direction)
    if not isinstance(direction_performance, dict):
        return Decimal("0")
    return _performance_entry_penalty(direction_performance)


def _objective_health(
    realized_performance: dict,
    short_model: dict | None,
    long_model: dict | None,
    short_threshold: Decimal,
    long_threshold: Decimal,
) -> dict:
    current_guard = realized_performance.get("current_guard")
    evaluated_performance = current_guard if isinstance(current_guard, dict) else realized_performance
    scope_label = "当前保护版真实闭环" if isinstance(current_guard, dict) else "真实闭环"
    realized_win_rate = _dict_decimal(evaluated_performance, "win_rate")
    realized_samples = int(evaluated_performance.get("sample_count") or 0)
    overall_win_rate = _dict_decimal(realized_performance, "win_rate")
    overall_samples = int(realized_performance.get("sample_count") or 0)
    projected_short = _selected_model_decimal(short_model, "projected_daily_trades")
    projected_long = _selected_model_decimal(long_model, "projected_daily_trades")
    projected_daily = max(projected_short or Decimal("0"), projected_long or Decimal("0"))
    realized_ok = realized_samples >= PERFORMANCE_MIN_TRADES and realized_win_rate is not None and realized_win_rate >= PERFORMANCE_TARGET_WIN_RATE
    projected_ok = PERFORMANCE_MIN_PROJECTED_DAILY_TRADES <= projected_daily <= Decimal("5")
    reasons = []
    if not realized_ok:
        if realized_samples < PERFORMANCE_MIN_TRADES:
            reasons.append(f"{scope_label}样本 {realized_samples} 单，少于 {PERFORMANCE_MIN_TRADES} 单。")
        else:
            reasons.append(f"{scope_label}胜率 {realized_win_rate:.2%} 未达 {PERFORMANCE_TARGET_WIN_RATE:.0%}。")
    if not projected_ok:
        reasons.append(f"模型预计日交易 {projected_daily:.2f} 单，不在 3-5 单目标内。")
    if realized_ok and projected_ok:
        reasons.append(f"{scope_label}胜率和模型日交易频率均达到目标。")
    return {
        "target_win_rate": PERFORMANCE_TARGET_WIN_RATE,
        "target_daily_trades_min": PERFORMANCE_MIN_PROJECTED_DAILY_TRADES,
        "target_daily_trades_max": Decimal("5"),
        "realized_sample_count": realized_samples,
        "realized_win_rate": realized_win_rate,
        "overall_realized_sample_count": overall_samples,
        "overall_realized_win_rate": overall_win_rate,
        "current_guard_sample_count": realized_samples if isinstance(current_guard, dict) else None,
        "current_guard_win_rate": realized_win_rate if isinstance(current_guard, dict) else None,
        "current_guard_version": current_guard.get("version") if isinstance(current_guard, dict) else None,
        "current_guard_start_ms": current_guard.get("start_ms") if isinstance(current_guard, dict) else None,
        "realized_ok": realized_ok,
        "projected_daily_trades": projected_daily,
        "projected_daily_trades_short": projected_short,
        "projected_daily_trades_long": projected_long,
        "projected_ok": projected_ok,
        "ready_for_goal": realized_ok and projected_ok,
        "short_threshold": short_threshold,
        "long_threshold": long_threshold,
        "reason": " ".join(reasons),
    }


def _selected_model_decimal(model: dict | None, key: str) -> Decimal | None:
    if not model:
        return None
    selected = model.get("selected")
    if not isinstance(selected, dict):
        return None
    return _dict_decimal(selected, key)


def _dict_decimal(data: dict, key: str) -> Decimal | None:
    value = data.get(key)
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


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
    entry_close_profit: Decimal,
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
            entry_close_profit=entry_close_profit,
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
        entry_close_profit=entry_close_profit,
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
    entry_close_profit: Decimal,
    settlement_adjustment: dict | None = None,
) -> dict:
    if current_edge > MAX_TRADABLE_ABS_SPREAD:
        return {
            "direction": direction.value,
            "direction_text": _direction_text(direction),
            "ready": False,
            "reason": f"当前价差 {current_edge} 超过黄金正常上限 {MAX_TRADABLE_ABS_SPREAD}，按停盘或错位报价过滤",
            "current_edge": current_edge,
            "threshold": threshold,
            "required_edge": threshold,
            "visible_trigger_edge": threshold,
            "locked_edge_floor": threshold + slippage_budget,
            "quantity_oz": qty,
            "binance_side": binance_side.value,
            "binance_price": limit_price,
            "mt4_follow_side": mt4_side.value,
            "expected_locked_edge": None,
            "estimated_exit_target_spread": None,
            "recent_low_spread": Decimal(str(spread_range["low"])) if spread_range.get("low") is not None else None,
            "exit_viable": False,
            "initial_close_profit_usd_per_oz": close_profit,
            "entry_viability_close_profit_usd_per_oz": entry_close_profit,
            "next_settlement_adjustment": settlement_adjustment,
            "mt4_reference_price": mt4_reference_price,
            "mt4_slippage_budget": slippage_budget,
            "mt4_exit_follow_budget": exit_follow_budget,
        }
    visible_trigger_edge = threshold
    locked_edge_floor = threshold + slippage_budget
    ready = current_edge >= visible_trigger_edge
    locked_edge = limit_price - mt4_reference_price if binance_side == Side.SELL else mt4_reference_price - limit_price
    adjustment_value = Decimal("0")
    if settlement_adjustment:
        adjustment_value = Decimal(str(settlement_adjustment["total"]))
    estimated_exit_target = max(Decimal("0"), locked_edge + (adjustment_value / qty) - slippage_budget - exit_follow_budget - entry_close_profit)
    recent_low = Decimal(str(spread_range["low"])) if spread_range.get("low") is not None else None
    exit_viable = recent_low is None or recent_low <= estimated_exit_target
    reason = "达到挂单触发位，可以挂币安限价等更优成交" if ready else f"当前价差未到挂单触发位 {visible_trigger_edge}"
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
        "required_edge": visible_trigger_edge,
        "visible_trigger_edge": visible_trigger_edge,
        "locked_edge_floor": locked_edge_floor,
        "quantity_oz": qty,
        "binance_side": binance_side.value,
        "binance_price": limit_price,
        "mt4_follow_side": mt4_side.value,
        "expected_locked_edge": locked_edge,
        "estimated_exit_target_spread": estimated_exit_target,
        "recent_low_spread": recent_low,
        "exit_viable": exit_viable,
        "initial_close_profit_usd_per_oz": close_profit,
        "entry_viability_close_profit_usd_per_oz": entry_close_profit,
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
    funding = _entry_binance_funding_estimate(settings, direction, qty, binance, metrics)
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
    settings: Settings,
    direction: PairDirection,
    qty: Decimal,
    binance: MarketQuote | None,
    metrics: PositionMetrics,
) -> Decimal | None:
    if not binance or metrics.binance_funding_rate is None:
        return None
    if not _entry_expected_to_cross_settlement(settings, metrics.binance_next_funding_time_ms):
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
    if not _entry_expected_to_cross_settlement(settings, next_rollover_time_ms):
        return None
    lots = qty / settings.mt4_lot_size_oz
    multiplier = _entry_mt4_swap_multiplier(settings, next_rollover_time_ms)
    return raw * lots * multiplier


def _entry_expected_to_cross_settlement(settings: Settings, settlement_time_ms: int | None) -> bool:
    if settlement_time_ms is None:
        return False
    if settings.max_pair_age_minutes <= 0:
        return True
    return settlement_time_ms - utc_now_ms() <= settings.max_pair_age_minutes * 60_000


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
    stale_weak = _stale_weak_exit_plan(settings, pair, metrics)
    negative_swap = _negative_swap_exit_plan(settings, pair, metrics)
    target_exit_spread = (
        loss_limit["target_exit_spread"]
        if loss_limit.get("active") and loss_limit.get("target_exit_spread") is not None
        else
        stale_weak["target_exit_spread"]
        if stale_weak.get("active") and stale_weak.get("target_exit_spread") is not None
        else
        negative_swap["target_exit_spread"]
        if negative_swap.get("active") and negative_swap.get("target_exit_spread") is not None
        else metrics.dynamic_close_spread
    )
    reason = "平仓目标来自真实均价、已产生费用和额外利润空间。"
    if loss_limit.get("active"):
        reason = loss_limit["reason"]
    elif stale_weak.get("active"):
        reason = stale_weak["reason"]
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
        "stale_weak": stale_weak,
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


def _stale_weak_exit_plan(settings: Settings, pair: OpenPair, metrics: PositionMetrics) -> dict:
    if settings.max_pair_age_minutes <= 0 or settings.max_pair_loss_usdt <= 0 or pair.quantity_oz <= 0:
        return {"active": False, "reason": "低质量旧仓释放未启用。"}
    if metrics.actual_entry_spread is None or metrics.estimated_close_net is None or metrics.profitable_spread_threshold is None:
        return {"active": False, "reason": "等待真实价差和净值后再判断低质量旧仓释放。"}
    if settings.open_min_edge <= 0 or metrics.actual_entry_spread >= settings.open_min_edge:
        return {"active": False, "reason": "当前组合不属于低于现行开仓标准的旧仓。"}
    critical_min_edge = _critical_weak_entry_min_edge(settings, metrics)
    if metrics.actual_entry_spread < critical_min_edge:
        target = max(Decimal("0"), metrics.profitable_spread_threshold + (settings.max_pair_loss_usdt / pair.quantity_oz))
        return {
            "active": True,
            "reason": (
                f"严重低质量进场：真实进场价差 {metrics.actual_entry_spread} 低于最低可盈利保护线 "
                f"{critical_min_edge}，立即进入受控离场，避免坏价差继续放大。"
            ),
            "target_exit_spread": target,
            "estimated_net": metrics.estimated_close_net,
            "critical_min_edge": critical_min_edge,
            "max_loss_usdt": settings.max_pair_loss_usdt,
        }
    age_ms = utc_now_ms() - int(pair.opened_ms)
    max_age_ms = settings.max_pair_age_minutes * 60_000
    if age_ms < max_age_ms:
        minutes_left = max(1, (max_age_ms - age_ms + 59_999) // 60_000)
        return {"active": False, "reason": f"低质量旧仓仍在等待回归，约 {minutes_left} 分钟后允许受控释放。"}
    if metrics.estimated_close_net <= -settings.max_pair_loss_usdt:
        return {
            "active": False,
            "reason": "已达到最大亏损风控，由最大亏损逻辑接管。",
            "max_loss_usdt": settings.max_pair_loss_usdt,
        }
    target = max(Decimal("0"), metrics.profitable_spread_threshold + (settings.max_pair_loss_usdt / pair.quantity_oz))
    return {
        "active": True,
        "reason": (
            f"低质量旧仓已超过 {settings.max_pair_age_minutes} 分钟且真实进场价差 "
            f"{metrics.actual_entry_spread} 低于当前开仓线 {settings.open_min_edge}，"
            f"允许在最大亏损 {settings.max_pair_loss_usdt} 内用币安限价释放仓位。"
        ),
        "target_exit_spread": target,
        "estimated_net": metrics.estimated_close_net,
        "max_loss_usdt": settings.max_pair_loss_usdt,
    }


def _critical_weak_entry_min_edge(settings: Settings, metrics: PositionMetrics) -> Decimal:
    close_profit = _entry_viability_close_profit(settings)
    exit_follow = metrics.exit_follow_buffer_usd_per_oz or Decimal("0")
    return close_profit + exit_follow


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
    close_profit = metrics.close_profit_usd_per_oz if metrics and metrics.close_profit_usd_per_oz is not None else _entry_viability_close_profit(settings)
    current_avg_edge = metrics.actual_entry_spread if metrics and metrics.actual_entry_spread is not None else pair.base_edge
    required_blended_edge = close_profit + exit_follow_budget
    required_locked_edge = None
    if current_avg_edge is not None and qty > 0:
        required_locked_edge = ((required_blended_edge * (pair.quantity_oz + qty)) - (current_avg_edge * pair.quantity_oz)) / qty
    actionable_trigger = trigger
    if required_locked_edge is not None:
        actionable_trigger = max(trigger, required_locked_edge - slippage_budget)
    average_protection_edge = current_avg_edge
    required_average_after_add = current_avg_edge
    add_improvement_buffer = None
    if average_protection_edge is not None:
        actionable_trigger = max(actionable_trigger, average_protection_edge)
        add_improvement_buffer = max(MIN_ADD_EDGE_IMPROVEMENT, slippage_budget / Decimal("2"))
        required_average_after_add = average_protection_edge + add_improvement_buffer
        required_locked_for_improvement = ((required_average_after_add * (pair.quantity_oz + qty)) - (average_protection_edge * pair.quantity_oz)) / qty
        actionable_trigger = max(actionable_trigger, required_locked_for_improvement - slippage_budget)
    if pair.direction == PairDirection.BINANCE_SHORT_MT4_LONG:
        edge = binance.ask - mt4.ask
        price = _round_up(max(binance.ask + settings.binance_entry_offset_usd, mt4.ask + actionable_trigger + slippage_budget), filters.tick_size)
        side, mt4_side, locked = Side.SELL, Side.BUY, price - mt4.ask
    else:
        edge = mt4.bid - binance.bid
        price = _round_down(min(binance.bid - settings.binance_entry_offset_usd, mt4.bid - actionable_trigger - slippage_budget), filters.tick_size)
        side, mt4_side, locked = Side.BUY, Side.SELL, mt4.bid - price
    if edge > MAX_TRADABLE_ABS_SPREAD:
        return {
            **data,
            "ready": False,
            "reason": f"当前补仓价差 {edge} 超过黄金正常上限 {MAX_TRADABLE_ABS_SPREAD}，按停盘或错位报价过滤。",
            "current_edge": edge,
            "next_actionable_trigger_edge": actionable_trigger,
            "required_blended_edge": required_blended_edge,
            "required_locked_edge": required_locked_edge,
            "average_protection_edge": average_protection_edge,
            "required_average_after_add": required_average_after_add,
            "add_improvement_buffer": add_improvement_buffer,
            "binance_side": side.value,
            "binance_price": price,
            "mt4_follow_side": mt4_side.value,
            "expected_locked_edge": locked,
            "estimated_blended_edge": None,
            "exit_viable": False,
            "mt4_slippage_budget": slippage_budget,
            "mt4_exit_follow_budget": exit_follow_budget,
        }
    blended_edge = None
    exit_viable = False
    if current_avg_edge is not None:
        blended_edge = ((current_avg_edge * pair.quantity_oz) + (locked * qty)) / (pair.quantity_oz + qty)
        exit_viable = (
            blended_edge >= required_blended_edge
            and blended_edge >= current_avg_edge
            and (required_average_after_add is None or blended_edge >= required_average_after_add)
        )
    ready = edge >= actionable_trigger and exit_viable
    reason = "达到补仓触发位，可以挂补仓限价单。"
    if edge < actionable_trigger:
        reason = "当前价差未到补仓保护触发位。"
    elif not exit_viable:
        if blended_edge is not None and required_average_after_add is not None and blended_edge < required_average_after_add:
            reason = "补仓后均价差改善不足，暂不放大仓位。"
        else:
            reason = "补仓后均价差仍不足以覆盖平仓缓冲和目标利润，暂不补仓。"
    return {**data,
        "ready": ready,
        "reason": reason,
        "current_edge": edge,
        "next_actionable_trigger_edge": actionable_trigger,
        "required_blended_edge": required_blended_edge,
        "required_locked_edge": required_locked_edge,
        "average_protection_edge": average_protection_edge,
        "required_average_after_add": required_average_after_add,
        "add_improvement_buffer": add_improvement_buffer,
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


def _entry_viability_close_profit(settings: Settings) -> Decimal:
    if 0 < settings.max_pair_age_minutes <= ENTRY_AGED_PROFIT_WINDOW_MINUTES:
        return min(settings.close_profit_usd_per_oz, settings.aged_close_profit_usd_per_oz)
    return settings.close_profit_usd_per_oz


def _entry_cooldown_minutes(settings: Settings) -> int:
    return max(0, settings.gold_v2_min_entry_interval_ms // 60_000)


def _model_spread_protection_budget(mt4_quote: MarketQuote | None) -> Decimal:
    return live_spread_usd_per_oz(mt4_quote) or Decimal("0")


def _mt4_slippage_budget(
    settings: Settings,
    storage: Storage,
    now_ms: int,
    mt4_quote: MarketQuote | None = None,
    mt4_bars: list[HistoryBar] | None = None,
    tick_move_budget: Decimal | None = None,
) -> Decimal:
    configured = slippage_budget_usd_per_oz(settings.mt4_slippage_points, XAU_POINT_VALUE, mt4_quote)
    base = max(configured, DEFAULT_SLIPPAGE_BUDGET)
    recent = tick_move_budget if tick_move_budget is not None else _mt4_recent_move_budget(mt4_bars or [])
    learned = mt4_entry_slippage_budget_usd_per_oz(storage, now_ms)
    return max(base + recent, learned)


def _mt4_exit_follow_budget(settings: Settings, storage: Storage, now_ms: int) -> Decimal:
    configured = Decimal(settings.mt4_slippage_points) * XAU_POINT_VALUE + settings.mt4_close_extra_buffer_usd
    learned = mt4_close_slippage_budget_usd_per_oz(storage, now_ms)
    return max(configured, learned)


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
