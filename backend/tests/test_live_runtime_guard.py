from decimal import Decimal
from datetime import datetime, timezone

from app.core.models import CashCarryOpportunity, DataSource, ExchangeBalance, ExchangeName, PositionSnapshot
from app.services.cash_carry_executor import CashCarryExecutor
from app.services.live_market_types import CashCarryScan
from app.services.live_read import LiveAccountSnapshot
from app.services.live_runtime import LiveRuntimeCache, STRATEGY_CASH


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
    allowed = runtime._allowed_single_exchange_open_exchanges()
    assert ExchangeName.GATE not in allowed
    assert ExchangeName.BYBIT in allowed


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

    assert runtime._auto_open_allowed(STRATEGY_CASH) is True
    assert runtime._allowed_single_exchange_open_exchanges() == {ExchangeName.BYBIT}


def test_runtime_marks_same_exchange_cash_carry_opportunities_blocked(tmp_path) -> None:
    state = tmp_path / "cash.json"
    state.write_text('{"positions":[{"exchange":"BITGET","symbol":"JCTUSDT","status":"open"}]}', encoding="utf-8")
    runtime = _runtime(tmp_path, cash_executor=CashCarryExecutor(state))

    scan = runtime._apply_cash_carry_open_scope(
        CashCarryScan(
            opportunities=[
                _cash_opportunity(ExchangeName.BITGET, "SKYAIUSDT", "3"),
                _cash_opportunity(ExchangeName.BYBIT, "XYZUSDT", "2"),
            ]
        )
    )

    assert [(item.exchange, item.symbol) for item in scan.opportunities] == [(ExchangeName.BYBIT, "XYZUSDT")]
    bitget = next(item for item in scan.candidates if item.exchange == ExchangeName.BITGET)
    assert "一所一币规则" in " / ".join(bitget.blocked_reasons)


def test_mt4_scan_uses_independent_slot(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    assert runtime._full_scan_slots.acquire(blocking=False) is True

    try:
        result, completed = runtime._run_guarded_scan(runtime._mt4_scan_slots, lambda: "mt4-ok", "fallback")
    finally:
        runtime._full_scan_slots.release()

    assert completed is True
    assert result == "mt4-ok"


def _runtime(tmp_path, cash_executor=None) -> LiveRuntimeCache:
    return LiveRuntimeCache(
        _LiveRead(),
        _Scanner(),
        _Mt4Scanner(),
        cash_carry_executor=cash_executor or CashCarryExecutor(tmp_path / "cash.json"),
    )


def _position(exchange: ExchangeName, symbol: str) -> PositionSnapshot:
    return PositionSnapshot(exchange=exchange, symbol=symbol, side="short", quantity=Decimal("1"), entry_price=Decimal("100"), mark_price=Decimal("101"), leverage=Decimal("2"), unrealized_pnl=Decimal("0"))


def _balance(exchange: ExchangeName) -> ExchangeBalance:
    return ExchangeBalance(exchange=exchange, equity_usdt=Decimal("1000"), available_usdt=Decimal("1000"), margin_used_usdt=Decimal("0"), updated_at=datetime.now(timezone.utc))


def _cash_opportunity(exchange: ExchangeName, symbol: str, net: str) -> CashCarryOpportunity:
    return CashCarryOpportunity(
        exchange=exchange,
        symbol=symbol,
        spot_price=Decimal("100"),
        perp_price=Decimal("101"),
        basis_pct=Decimal("1"),
        funding_rate_pct=Decimal("0.01"),
        quantity=Decimal("1"),
        spot_volume_24h_usdt=Decimal("1000000"),
        perp_volume_24h_usdt=Decimal("1000000"),
        estimated_basis_profit=Decimal("1"),
        estimated_funding_income=Decimal("0.01"),
        estimated_open_close_fee=Decimal("0.2"),
        estimated_net_profit=Decimal(net),
        blocked_reasons=[],
        data_source=DataSource.LIVE,
        updated_at=datetime.now(timezone.utc),
    )


class _LiveRead:
    pass


class _Scanner:
    pass


class _Mt4Scanner:
    def scan(self, settings):
        return [], [], []
