from __future__ import annotations

from decimal import Decimal

from app.models import HistoryBar, MarketQuote


def live_spread_usd_per_oz(quote: MarketQuote | None) -> Decimal | None:
    if not quote:
        return None
    spread = quote.ask - quote.bid
    return spread if spread > 0 else Decimal("0")


def spread_cost_usd(quote: MarketQuote | None, quantity_oz: Decimal) -> Decimal | None:
    spread = live_spread_usd_per_oz(quote)
    if spread is None:
        return None
    return spread * quantity_oz


def slippage_budget_usd_per_oz(slippage_points: int, point: Decimal, quote: MarketQuote | None) -> Decimal:
    base = Decimal(slippage_points) * point
    spread = live_spread_usd_per_oz(quote)
    return base + (spread or Decimal("0"))


def recent_move_budget_usd_per_oz(
    bars: list[HistoryBar],
    percentile: int = 70,
    min_points: int = 8,
) -> Decimal:
    if len(bars) < min_points:
        return Decimal("0")
    ordered = sorted(bars, key=lambda bar: bar.open_time_ms)
    moves = [abs(curr.close - prev.close) for prev, curr in zip(ordered, ordered[1:])]
    if not moves:
        return Decimal("0")
    moves.sort()
    bounded_percentile = min(max(percentile, 0), 100)
    index = ((len(moves) - 1) * bounded_percentile) // 100
    return moves[index]
