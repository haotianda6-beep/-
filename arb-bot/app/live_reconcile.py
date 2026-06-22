from __future__ import annotations

from decimal import Decimal
from typing import Literal

from app.models import Mt4Position, OpenPair


LiveReconcileAction = Literal["clear", "pause"]


def open_pair_live_reconcile_action(
    open_pair: OpenPair | None,
    binance_position_qty: Decimal,
    mt4_positions: list[Mt4Position],
    mt4_symbol: str,
) -> LiveReconcileAction | None:
    if open_pair is None:
        return None
    mt4_symbol_positions = [position for position in mt4_positions if position.symbol == mt4_symbol]
    binance_flat = binance_position_qty == 0
    mt4_flat = not mt4_symbol_positions
    if binance_flat and mt4_flat:
        return "clear"
    if binance_flat or mt4_flat:
        return "pause"
    return None
