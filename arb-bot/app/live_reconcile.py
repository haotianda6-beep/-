from __future__ import annotations

from decimal import Decimal
from typing import Literal

from app.models import Mt4Position, OpenPair


LiveReconcileAction = Literal["clear", "pause"]
OrphanLiveAction = Literal["binance", "mt4", "both"]


def is_transient_live_reconcile_error(error_text: str) -> bool:
    return (
        "-1021" in error_text
        or "recvWindow" in error_text
        or "-1003" in error_text
        or "Too many requests" in error_text
        or "418" in error_text
        or "I'm a teapot" in error_text
    )


def open_pair_live_reconcile_action(
    open_pair: OpenPair | None,
    binance_position_qty: Decimal,
    mt4_positions: list[Mt4Position],
    mt4_symbol: str,
    mt4_lot_size_oz: Decimal = Decimal("100"),
    tolerance: Decimal = Decimal("0.0001"),
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
    binance_should_be_short = open_pair.direction.name == "BINANCE_SHORT_MT4_LONG"
    if binance_should_be_short and binance_position_qty >= 0:
        return "pause"
    if not binance_should_be_short and binance_position_qty <= 0:
        return "pause"
    if abs(abs(binance_position_qty) - open_pair.quantity_oz) > tolerance:
        return "pause"
    expected_mt4_side = "BUY" if binance_should_be_short else "SELL"
    if any(position.side.value != expected_mt4_side for position in mt4_symbol_positions):
        return "pause"
    mt4_qty = sum((position.lots for position in mt4_symbol_positions), Decimal("0")) * mt4_lot_size_oz
    if abs(mt4_qty - open_pair.quantity_oz) > tolerance:
        return "pause"
    return None


def open_pair_sync_grace_active(
    open_pair: OpenPair | None,
    binance_position_qty: Decimal,
    mt4_positions: list[Mt4Position],
    mt4_symbol: str,
    now_ms: int,
    grace_ms: int,
    mt4_lot_size_oz: Decimal = Decimal("100"),
    tolerance: Decimal = Decimal("0.0001"),
    sync_started_ms: int | None = None,
) -> bool:
    started_ms = int(open_pair.opened_ms) if open_pair is not None and sync_started_ms is None else sync_started_ms
    if open_pair is None or started_ms is None or grace_ms <= 0 or now_ms - int(started_ms) > grace_ms:
        return False
    mt4_symbol_positions = [position for position in mt4_positions if position.symbol == mt4_symbol]
    binance_should_be_short = open_pair.direction.name == "BINANCE_SHORT_MT4_LONG"
    if binance_should_be_short and binance_position_qty >= 0:
        return False
    if not binance_should_be_short and binance_position_qty <= 0:
        return False
    if abs(abs(binance_position_qty) - open_pair.quantity_oz) > tolerance:
        return False
    expected_mt4_side = "BUY" if binance_should_be_short else "SELL"
    if any(position.side.value != expected_mt4_side for position in mt4_symbol_positions):
        return False
    mt4_qty = sum((position.lots for position in mt4_symbol_positions), Decimal("0")) * mt4_lot_size_oz
    return mt4_qty < open_pair.quantity_oz - tolerance


def open_pair_binance_restore_quantity(
    open_pair: OpenPair | None,
    binance_position_qty: Decimal,
    mt4_positions: list[Mt4Position],
    mt4_symbol: str,
    mt4_lot_size_oz: Decimal = Decimal("100"),
    tolerance: Decimal = Decimal("0.0001"),
) -> Decimal | None:
    if open_pair is None:
        return None
    mt4_symbol_positions = [position for position in mt4_positions if position.symbol == mt4_symbol]
    if not mt4_symbol_positions:
        return None
    binance_should_be_short = open_pair.direction.name == "BINANCE_SHORT_MT4_LONG"
    if binance_should_be_short and binance_position_qty > 0:
        return None
    if not binance_should_be_short and binance_position_qty < 0:
        return None
    expected_mt4_side = "BUY" if binance_should_be_short else "SELL"
    if any(position.side.value != expected_mt4_side for position in mt4_symbol_positions):
        return None
    mt4_qty = sum((position.lots for position in mt4_symbol_positions), Decimal("0")) * mt4_lot_size_oz
    if abs(mt4_qty - open_pair.quantity_oz) > tolerance:
        return None
    live_qty = abs(binance_position_qty)
    missing_qty = open_pair.quantity_oz - live_qty
    if missing_qty <= tolerance:
        return None
    return missing_qty


def orphan_live_position_action(
    open_pair: OpenPair | None,
    binance_position_qty: Decimal,
    mt4_positions: list[Mt4Position],
    mt4_symbol: str,
) -> OrphanLiveAction | None:
    if open_pair is not None:
        return None
    mt4_symbol_positions = [position for position in mt4_positions if position.symbol == mt4_symbol and position.lots > 0]
    has_binance = binance_position_qty != 0
    has_mt4 = bool(mt4_symbol_positions)
    if has_binance and has_mt4:
        return "both"
    if has_binance:
        return "binance"
    if has_mt4:
        return "mt4"
    return None
