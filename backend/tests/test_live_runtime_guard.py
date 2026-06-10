from decimal import Decimal
from datetime import datetime, timezone

from app.core.models import ExchangeBalance, ExchangeName, PositionSnapshot
from app.services.cash_carry_executor import CashCarryExecutor
from app.services.cross_spread_executor import CrossSpreadExecutor
from app.services.live_read import LiveAccountSnapshot
from app.services.live_runtime import LiveRuntimeCache, STRATEGY_CASH, STRATEGY_CROSS, STRATEGY_REVERSE
from app.services.reverse_cash_carry_executor import ReverseCashCarryExecutor


def test_runtime_open_guard_blocks_until_account_is_clean(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime._account = LiveAccountSnapshot(issues=["账户数据后台加载中"])

    assert runtime._auto_open_allowed(STRATEGY_CASH) is False


def test_runtime_open_guard_blocks_when_live_position_is_untracked(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime._account = LiveAccountSnapshot(
        balances=[_balance(ExchangeName.GATE)],
        positions=[
            PositionSnapshot(
                exchange=ExchangeName.GATE,
                symbol="ABCUSDT",
                side="short",
                quantity=Decimal("1"),
                entry_price=Decimal("100"),
                mark_price=Decimal("101"),
                leverage=Decimal("2"),
                unrealized_pnl=Decimal("0"),
            )
        ]
    )

    assert runtime._auto_open_allowed(STRATEGY_CASH) is False


def test_runtime_open_guard_allows_single_exchange_strategies_on_other_exchanges(tmp_path) -> None:
    state = tmp_path / "cash.json"
    state.write_text('{"positions":[{"exchange":"GATE","symbol":"ABCUSDT","status":"mismatch"}]}', encoding="utf-8")
    runtime = _runtime(tmp_path, cash_executor=CashCarryExecutor(state))
    runtime._account = LiveAccountSnapshot(
        balances=[_balance(ExchangeName.GATE), _balance(ExchangeName.BYBIT)],
        positions=[
            PositionSnapshot(
                exchange=ExchangeName.GATE,
                symbol="ABCUSDT",
                side="short",
                quantity=Decimal("1"),
                entry_price=Decimal("100"),
                mark_price=Decimal("101"),
                leverage=Decimal("2"),
                unrealized_pnl=Decimal("0"),
            )
        ]
    )

    assert runtime._auto_open_allowed(STRATEGY_CASH) is True
    assert runtime._auto_open_allowed(STRATEGY_CROSS) is False
    assert runtime._auto_open_allowed(STRATEGY_REVERSE) is True
    allowed = runtime._allowed_single_exchange_open_exchanges()
    assert ExchangeName.GATE not in allowed
    assert ExchangeName.BYBIT in allowed


def test_runtime_open_guard_blocks_when_cross_state_is_active(tmp_path) -> None:
    state = tmp_path / "cross.json"
    state.write_text(
        '{"positions":[{"id":"1","symbol":"ABCUSDT","long_exchange":"BINANCE","short_exchange":"OKX","quantity":"1","long_entry_price":"100","short_entry_price":"102","opened_at":"2026-06-09T00:00:00+00:00","status":"open"}]}',
        encoding="utf-8",
    )
    runtime = _runtime(tmp_path, cross_executor=CrossSpreadExecutor(state))
    runtime._account = LiveAccountSnapshot(balances=[_balance(ExchangeName.BINANCE), _balance(ExchangeName.OKX)])

    assert runtime._auto_open_allowed(STRATEGY_CROSS) is True
    assert runtime._auto_open_allowed(STRATEGY_CASH) is False


def test_runtime_single_exchange_lock_combines_forward_and_reverse(tmp_path) -> None:
    cash_state = tmp_path / "cash.json"
    reverse_state = tmp_path / "reverse.json"
    cash_state.write_text('{"positions":[{"exchange":"GATE","symbol":"ABCUSDT","status":"open"}]}', encoding="utf-8")
    reverse_state.write_text(
        '{"positions":[{"id":"r1","exchange":"BYBIT","symbol":"XYZUSDT","base_asset":"XYZ","quantity":"1","borrowed_quantity":"1","spot_entry_price":"1","perp_entry_price":"0.9","opened_at":"2026-06-09T00:00:00+00:00","status":"open"}]}',
        encoding="utf-8",
    )
    runtime = _runtime(tmp_path, cash_executor=CashCarryExecutor(cash_state), reverse_executor=ReverseCashCarryExecutor(reverse_state))
    runtime._account = LiveAccountSnapshot(
        balances=[_balance(ExchangeName.GATE), _balance(ExchangeName.BYBIT), _balance(ExchangeName.OKX)],
        positions=[
            _position(ExchangeName.GATE, "ABCUSDT"),
            _position(ExchangeName.BYBIT, "XYZUSDT"),
        ]
    )

    allowed = runtime._allowed_single_exchange_open_exchanges()

    assert ExchangeName.GATE not in allowed
    assert ExchangeName.BYBIT not in allowed
    assert ExchangeName.OKX in allowed


def test_runtime_open_guard_allows_when_no_active_position(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime._account = LiveAccountSnapshot(balances=[_balance(ExchangeName.BYBIT)])

    assert runtime._auto_open_allowed(STRATEGY_CASH) is True


def test_runtime_single_exchange_open_ignores_unrelated_account_issue(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime._account = LiveAccountSnapshot(
        balances=[_balance(ExchangeName.BYBIT)],
        issues=["OKX: 接口临时异常"],
    )

    assert runtime._auto_open_allowed(STRATEGY_REVERSE) is True
    assert runtime._auto_open_allowed(STRATEGY_CROSS) is False
    assert runtime._allowed_single_exchange_open_exchanges() == {ExchangeName.BYBIT}


def _runtime(tmp_path, cash_executor=None, cross_executor=None, reverse_executor=None) -> LiveRuntimeCache:
    return LiveRuntimeCache(
        _LiveRead(),
        _Scanner(),
        _Scanner(),
        _Scanner(),
        _Mt4Scanner(),
        cross_spread_executor=cross_executor or CrossSpreadExecutor(tmp_path / "cross.json"),
        cash_carry_executor=cash_executor or CashCarryExecutor(tmp_path / "cash.json"),
        reverse_cash_carry_executor=reverse_executor or ReverseCashCarryExecutor(tmp_path / "reverse.json"),
    )


def _position(exchange: ExchangeName, symbol: str) -> PositionSnapshot:
    return PositionSnapshot(exchange=exchange, symbol=symbol, side="short", quantity=Decimal("1"), entry_price=Decimal("100"), mark_price=Decimal("101"), leverage=Decimal("2"), unrealized_pnl=Decimal("0"))


def _balance(exchange: ExchangeName) -> ExchangeBalance:
    return ExchangeBalance(exchange=exchange, equity_usdt=Decimal("1000"), available_usdt=Decimal("1000"), margin_used_usdt=Decimal("0"), updated_at=datetime.now(timezone.utc))


class _LiveRead:
    pass


class _Scanner:
    pass


class _Mt4Scanner:
    def scan(self, settings):
        return [], [], []
