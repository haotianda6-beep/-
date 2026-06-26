from __future__ import annotations

from decimal import Decimal

MIN_MODEL_TRADES = 1
TARGET_DAILY_TRADES = Decimal("4")
MIN_DAILY_TRADES = Decimal("3")
MAX_DAILY_TRADES = Decimal("5")
MIN_TARGET_WIN_RATE = Decimal("0.70")
PERCENTILES = (55, 60, 65, 70, 75, 80, 85, 90, 92, 94, 95, 96, 97, 98, 99, 100)


def build_entry_model(
    values: list[Decimal],
    manual_min: Decimal,
    slippage_budget: Decimal,
    exit_follow_budget: Decimal,
    close_profit: Decimal,
    max_hold_minutes: int,
    min_points: int,
) -> dict:
    if len(values) < min_points:
        return {
            "enabled": False,
            "reason": "样本不足，暂不启用自动阈值模型。",
            "points": len(values),
            "suggested_threshold": None,
        }
    candidates = _candidate_thresholds(values, manual_min)
    results = [
        _simulate_candidate(
            values=values,
            threshold=threshold,
            slippage_budget=slippage_budget,
            exit_follow_budget=exit_follow_budget,
            close_profit=close_profit,
            max_hold_minutes=max_hold_minutes,
        )
        for threshold in candidates
    ]
    selected = _select_candidate(results)
    if selected is None:
        return {
            "enabled": True,
            "reason": "最近样本没有证明 70% 以上回归胜率，沿用区间阈值。",
            "points": len(values),
            "suggested_threshold": None,
            "candidates": results,
        }
    return {
        "enabled": True,
        "reason": _selection_reason(selected),
        "points": len(values),
        "suggested_threshold": selected["threshold"],
        "selected": selected,
        "candidates": results,
    }


def _candidate_thresholds(values: list[Decimal], manual_min: Decimal) -> list[Decimal]:
    sorted_values = sorted(values)
    thresholds = {manual_min}
    for percentile in PERCENTILES:
        thresholds.add(max(manual_min, _percentile(sorted_values, percentile)))
    return sorted(thresholds)


def _percentile(sorted_values: list[Decimal], percentile: int) -> Decimal:
    if not sorted_values:
        return Decimal("0")
    index = int((len(sorted_values) - 1) * percentile / 100)
    return sorted_values[index]


def _simulate_candidate(
    values: list[Decimal],
    threshold: Decimal,
    slippage_budget: Decimal,
    exit_follow_budget: Decimal,
    close_profit: Decimal,
    max_hold_minutes: int,
) -> dict:
    hold = max(1, max_hold_minutes)
    target_exit = max(Decimal("0"), threshold + slippage_budget - slippage_budget - exit_follow_budget - close_profit)
    wins = 0
    losses = 0
    index = 0
    while index < len(values):
        if values[index] < threshold:
            index += 1
            continue
        end = min(len(values) - 1, index + hold)
        exit_index = _first_exit_index(values, index + 1, end, target_exit)
        if exit_index is None:
            losses += 1
            index = end + 1
        else:
            wins += 1
            index = exit_index + 1
    trades = wins + losses
    win_rate = Decimal(wins) / Decimal(trades) if trades else None
    projected_daily_trades = Decimal(trades) * Decimal(1440) / Decimal(len(values)) if values else Decimal("0")
    return {
        "threshold": threshold,
        "target_exit_spread": target_exit,
        "wins": wins,
        "losses": losses,
        "trades": trades,
        "win_rate": win_rate,
        "projected_daily_trades": projected_daily_trades,
    }


def _first_exit_index(values: list[Decimal], start: int, end: int, target_exit: Decimal) -> int | None:
    for index in range(start, end + 1):
        if values[index] <= target_exit:
            return index
    return None


def _select_candidate(results: list[dict]) -> dict | None:
    eligible = [
        result
        for result in results
        if result["trades"] >= MIN_MODEL_TRADES
        and result["win_rate"] is not None
        and result["win_rate"] >= MIN_TARGET_WIN_RATE
    ]
    if not eligible:
        return None
    target_rate = [result for result in eligible if _daily_trade_in_target(result)]
    if target_rate:
        return max(target_rate, key=_target_candidate_score)
    return max(eligible, key=_candidate_score)


def _candidate_score(result: dict) -> tuple[Decimal, Decimal, Decimal]:
    projected = Decimal(str(result["projected_daily_trades"]))
    win_rate = Decimal(str(result["win_rate"]))
    trade_gap = abs(projected - TARGET_DAILY_TRADES)
    return (win_rate, -trade_gap, -Decimal(str(result["threshold"])))


def _target_candidate_score(result: dict) -> tuple[Decimal, Decimal, Decimal]:
    projected = Decimal(str(result["projected_daily_trades"]))
    win_rate = Decimal(str(result["win_rate"]))
    trade_gap = abs(projected - TARGET_DAILY_TRADES)
    return (win_rate, -trade_gap, -Decimal(str(result["threshold"])))


def _daily_trade_in_target(result: dict) -> bool:
    projected = Decimal(str(result["projected_daily_trades"]))
    return MIN_DAILY_TRADES <= projected <= MAX_DAILY_TRADES


def _selection_reason(selected: dict) -> str:
    if _daily_trade_in_target(selected):
        return "已按最近样本选择满足70%胜率且接近日开3-5单的入场阈值。"
    return "已按最近样本选择70%胜率以上的入场阈值，但预计开单频率不在3-5单/天内。"
