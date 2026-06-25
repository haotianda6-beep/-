from decimal import Decimal

from app.live_reconcile import (
    is_transient_live_reconcile_error,
    open_pair_binance_restore_quantity,
    open_pair_live_reconcile_action,
    orphan_live_position_action,
)
from app.models import Mt4Position, OpenPair, PairDirection, Side


def test_open_pair_reconcile_clears_when_binance_and_mt4_are_flat() -> None:
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("2"),
        binance_entry_price=Decimal("4152.68"),
        mt4_entry_price=Decimal("4149.47"),
        binance_order_id="7262226459",
        mt4_tickets=[76804334, 76805260],
    )

    action = open_pair_live_reconcile_action(pair, Decimal("0"), [], "XAUUSD")

    assert action == "clear"


def test_open_pair_reconcile_pauses_when_only_binance_is_flat() -> None:
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("2"),
        binance_entry_price=Decimal("4152.68"),
        mt4_entry_price=Decimal("4149.47"),
        binance_order_id="7262226459",
        mt4_tickets=[76804334, 76805260],
    )

    action = open_pair_live_reconcile_action(
        pair,
        Decimal("0"),
        [Mt4Position(ticket=76804334, symbol="XAUUSD", side=Side.BUY, lots=Decimal("0.01"), open_price=Decimal("4149.47"))],
        "XAUUSD",
    )

    assert action == "pause"


def test_open_pair_reconcile_pauses_when_binance_quantity_mismatches() -> None:
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("2"),
        binance_entry_price=Decimal("4152.68"),
        mt4_entry_price=Decimal("4149.47"),
        binance_order_id="7262226459",
        mt4_tickets=[76804334, 76805260],
    )

    action = open_pair_live_reconcile_action(
        pair,
        Decimal("-1"),
        [
            Mt4Position(ticket=76804334, symbol="XAUUSD", side=Side.BUY, lots=Decimal("0.01"), open_price=Decimal("4149.47")),
            Mt4Position(ticket=76805260, symbol="XAUUSD", side=Side.BUY, lots=Decimal("0.01"), open_price=Decimal("4149.47")),
        ],
        "XAUUSD",
    )

    assert action == "pause"


def test_open_pair_restore_quantity_when_binance_is_underfilled_but_mt4_matches() -> None:
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("1"),
        binance_entry_price=Decimal("4152.68"),
        mt4_entry_price=Decimal("4149.47"),
        binance_order_id="7262226459",
        mt4_tickets=[76804334],
    )

    restore_qty = open_pair_binance_restore_quantity(
        pair,
        Decimal("-0.684"),
        [Mt4Position(ticket=76804334, symbol="XAUUSD", side=Side.BUY, lots=Decimal("0.01"), open_price=Decimal("4149.47"))],
        "XAUUSD",
    )

    assert restore_qty == Decimal("0.316")


def test_open_pair_reconcile_pauses_when_mt4_quantity_mismatches() -> None:
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("2"),
        binance_entry_price=Decimal("4152.68"),
        mt4_entry_price=Decimal("4149.47"),
        binance_order_id="7262226459",
        mt4_tickets=[76804334, 76805260],
    )

    action = open_pair_live_reconcile_action(
        pair,
        Decimal("-2"),
        [Mt4Position(ticket=76804334, symbol="XAUUSD", side=Side.BUY, lots=Decimal("0.01"), open_price=Decimal("4149.47"))],
        "XAUUSD",
    )

    assert action == "pause"


def test_open_pair_reconcile_allows_matching_live_positions() -> None:
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("2"),
        binance_entry_price=Decimal("4152.68"),
        mt4_entry_price=Decimal("4149.47"),
        binance_order_id="7262226459",
        mt4_tickets=[76804334, 76805260],
    )

    action = open_pair_live_reconcile_action(
        pair,
        Decimal("-2"),
        [
            Mt4Position(ticket=76804334, symbol="XAUUSD", side=Side.BUY, lots=Decimal("0.01"), open_price=Decimal("4149.47")),
            Mt4Position(ticket=76805260, symbol="XAUUSD", side=Side.BUY, lots=Decimal("0.01"), open_price=Decimal("4149.47")),
        ],
        "XAUUSD",
    )

    assert action is None


def test_open_pair_reconcile_ignores_other_mt4_symbols_when_xau_is_flat() -> None:
    pair = OpenPair(
        direction=PairDirection.BINANCE_SHORT_MT4_LONG,
        quantity_oz=Decimal("2"),
        binance_entry_price=Decimal("4152.68"),
        mt4_entry_price=Decimal("4149.47"),
        binance_order_id="7262226459",
        mt4_tickets=[76804334, 76805260],
    )

    action = open_pair_live_reconcile_action(
        pair,
        Decimal("0"),
        [Mt4Position(ticket=1, symbol="ETHUSD", side=Side.BUY, lots=Decimal("0.01"), open_price=Decimal("3000"))],
        "XAUUSD",
    )

    assert action == "clear"


def test_binance_recv_window_error_is_transient() -> None:
    assert is_transient_live_reconcile_error('{"code":-1021,"msg":"Timestamp for this request is outside of the recvWindow."}')
    assert is_transient_live_reconcile_error('{"code":-1003,"msg":"Too many requests; current limit of IP is 2400 requests per minute."}')
    assert is_transient_live_reconcile_error("418 I'm a teapot")
    assert not is_transient_live_reconcile_error('{"code":-2015,"msg":"Invalid API-key, IP, or permissions."}')


def test_orphan_live_position_action_blocks_binance_leftover_without_pair() -> None:
    assert orphan_live_position_action(None, Decimal("-1"), [], "XAUUSD") == "binance"


def test_orphan_live_position_action_blocks_mt4_leftover_without_pair() -> None:
    action = orphan_live_position_action(
        None,
        Decimal("0"),
        [Mt4Position(ticket=1, symbol="XAUUSD", side=Side.BUY, lots=Decimal("0.01"), open_price=Decimal("3990"))],
        "XAUUSD",
    )

    assert action == "mt4"


def test_orphan_live_position_action_blocks_both_leftover_without_pair() -> None:
    action = orphan_live_position_action(
        None,
        Decimal("-1"),
        [Mt4Position(ticket=1, symbol="XAUUSD", side=Side.BUY, lots=Decimal("0.01"), open_price=Decimal("3990"))],
        "XAUUSD",
    )

    assert action == "both"


def test_orphan_live_position_action_allows_flat_without_pair() -> None:
    action = orphan_live_position_action(
        None,
        Decimal("0"),
        [Mt4Position(ticket=1, symbol="ETHUSD", side=Side.BUY, lots=Decimal("0.01"), open_price=Decimal("3000"))],
        "XAUUSD",
    )

    assert action is None
