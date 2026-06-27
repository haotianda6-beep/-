from decimal import Decimal
from datetime import datetime, timezone

from app.core.models import BotSettings, CashCarryOpportunity, DataSource, ExchangeBalance, ExchangeName, PositionSnapshot
from app.services.cash_carry_executor import CashCarryExecutor
from app.services.cash_carry_scope import CASH_CARRY_INTERNAL_CANDIDATE_LIMIT
from app.services.live_market_types import CashCarryScan
from app.services.live_read import LiveAccountSnapshot
from app.services.live_runtime import LiveRuntimeCache, STRATEGY_CASH, TickerSubscription
from app.services.ws_ticker_cache import WSTickerCache


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
        balances=[_balance(ExchangeName.GATE), _balance(ExchangeName.BITGET), _balance(ExchangeName.BYBIT)],
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
    runtime._settings = BotSettings(cash_carry_max_positions_per_exchange=1)

    assert runtime._auto_open_allowed(STRATEGY_CASH) is True
    allowed = runtime._allowed_single_exchange_open_exchanges()
    assert ExchangeName.GATE not in allowed
    assert ExchangeName.BITGET in allowed
    assert ExchangeName.BYBIT not in allowed


def test_runtime_open_guard_allows_when_no_active_position(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime._account = LiveAccountSnapshot(balances=[_balance(ExchangeName.BITGET)])

    assert runtime._auto_open_allowed(STRATEGY_CASH) is True


def test_runtime_open_guard_blocks_when_only_removed_exchanges_are_healthy(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime._account = LiveAccountSnapshot(balances=[_balance(ExchangeName.BYBIT)])

    assert runtime._auto_open_allowed(STRATEGY_CASH) is False
    assert runtime._allowed_single_exchange_open_exchanges() == set()


def test_runtime_single_exchange_open_ignores_unrelated_account_issue(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime._account = LiveAccountSnapshot(
        balances=[_balance(ExchangeName.BITGET), _balance(ExchangeName.BYBIT)],
        issues=["OKX: 接口临时异常"],
    )

    assert runtime._auto_open_allowed(STRATEGY_CASH) is True
    assert runtime._allowed_single_exchange_open_exchanges() == {ExchangeName.BITGET}


def test_runtime_ignores_untracked_positions_outside_cash_carry_exchanges(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime._account = LiveAccountSnapshot(
        balances=[_balance(ExchangeName.GATE)],
        positions=[_position(ExchangeName.BYBIT, "OTHERUSDT")],
    )

    assert runtime._auto_open_allowed(STRATEGY_CASH) is True


def test_runtime_allows_same_exchange_cash_carry_opportunities_when_slot_is_available(tmp_path) -> None:
    state = tmp_path / "cash.json"
    state.write_text('{"positions":[{"exchange":"BITGET","symbol":"JCTUSDT","status":"open"}]}', encoding="utf-8")
    runtime = _runtime(tmp_path, cash_executor=CashCarryExecutor(state))
    runtime._settings = BotSettings(cash_carry_max_positions_per_exchange=2)

    scan = runtime._apply_cash_carry_open_scope(
        CashCarryScan(
            opportunities=[
                _cash_opportunity(ExchangeName.BITGET, "SKYAIUSDT", "3"),
                _cash_opportunity(ExchangeName.GATE, "XYZUSDT", "2"),
            ]
        )
    )

    assert [(item.exchange, item.symbol) for item in scan.opportunities] == [
        (ExchangeName.BITGET, "SKYAIUSDT"),
        (ExchangeName.GATE, "XYZUSDT"),
    ]
    bitget = next(item for item in scan.candidates if item.exchange == ExchangeName.BITGET)
    assert bitget.blocked_reasons == []


def test_runtime_marks_same_exchange_cash_carry_opportunities_blocked_when_slots_are_full(tmp_path) -> None:
    state = tmp_path / "cash.json"
    state.write_text('{"positions":[{"exchange":"BITGET","symbol":"JCTUSDT","status":"open"}]}', encoding="utf-8")
    runtime = _runtime(tmp_path, cash_executor=CashCarryExecutor(state))
    runtime._settings = BotSettings(cash_carry_max_positions_per_exchange=1)

    scan = runtime._apply_cash_carry_open_scope(
        CashCarryScan(
            opportunities=[
                _cash_opportunity(ExchangeName.BITGET, "SKYAIUSDT", "3"),
                _cash_opportunity(ExchangeName.GATE, "XYZUSDT", "2"),
            ]
        )
    )

    assert [(item.exchange, item.symbol) for item in scan.opportunities] == [(ExchangeName.GATE, "XYZUSDT")]
    bitget = next(item for item in scan.candidates if item.exchange == ExchangeName.BITGET)
    assert "持仓槽位已满" in " / ".join(bitget.blocked_reasons)


def test_runtime_rebuild_keeps_internal_candidate_pool(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    rows = [
        _cash_opportunity(ExchangeName.GATE, f"ABC{i}USDT", str(i))
        for i in range(CASH_CARRY_INTERNAL_CANDIDATE_LIMIT + 10)
    ]

    scan = runtime._apply_cash_carry_open_scope(CashCarryScan(candidates=rows))

    assert len(scan.candidates) == CASH_CARRY_INTERNAL_CANDIDATE_LIMIT


def test_runtime_default_ws_capacity_covers_internal_candidate_pool(tmp_path) -> None:
    runtime = _runtime(tmp_path)

    assert runtime.ticker_cache.max_symbols_per_stream == CASH_CARRY_INTERNAL_CANDIDATE_LIMIT


def test_runtime_replaces_cash_carry_ws_watchlist(tmp_path) -> None:
    cache = _TickerCache()
    runtime = _runtime(tmp_path, ticker_cache=cache)

    runtime._subscribe_cash_carry([
        TickerSubscription(ExchangeName.GATE, "AAAUSDT", "AAA/USDT", "AAA/USDT:USDT"),
        TickerSubscription(ExchangeName.GATE, "BBBUSDT", "BBB/USDT", "BBB/USDT:USDT"),
    ])
    runtime._subscribe_cash_carry([
        TickerSubscription(ExchangeName.GATE, "CCCUSDT", "CCC/USDT", "CCC/USDT:USDT"),
    ])

    assert cache.subscriptions[(ExchangeName.GATE, "spot")] == {"CCCUSDT": "CCC/USDT"}
    assert cache.subscriptions[(ExchangeName.GATE, "swap")] == {"CCCUSDT": "CCC/USDT:USDT"}
    assert cache.subscriptions[(ExchangeName.BITGET, "spot")] == {}
    assert cache.subscriptions[(ExchangeName.BITGET, "swap")] == {}


def test_ws_ticker_cache_replace_subscriptions_prunes_stale_symbols(monkeypatch) -> None:
    cache = WSTickerCache(max_symbols_per_stream=2)
    monkeypatch.setattr(cache, "_run_thread", lambda *_args: None)

    cache.replace_subscriptions(ExchangeName.GATE, "spot", {"AAAUSDT": "AAA/USDT", "BBBUSDT": "BBB/USDT", "CCCUSDT": "CCC/USDT"})
    cache._tickers[(ExchangeName.GATE, "spot", "AAAUSDT")] = (datetime.now(timezone.utc), {"last": 1})
    cache._tickers[(ExchangeName.GATE, "spot", "OLDUSDT")] = (datetime.now(timezone.utc), {"last": 2})

    cache.replace_subscriptions(ExchangeName.GATE, "spot", {"BBBUSDT": "BBB/USDT"})

    assert cache._subscriptions[(ExchangeName.GATE, "spot")] == {"BBBUSDT": "BBB/USDT"}
    assert (ExchangeName.GATE, "spot", "AAAUSDT") not in cache._tickers
    assert (ExchangeName.GATE, "spot", "OLDUSDT") not in cache._tickers


def test_runtime_marks_recent_depth_failed_symbol_as_candidate(tmp_path) -> None:
    state = tmp_path / "cash.json"
    blocked_at = datetime.now(timezone.utc).isoformat()
    state.write_text(
        f'{{"positions":[],"last_result":{{"status":"blocked_by_depth","exchange":"GATE","symbol":"ABCUSDT","reason":"深度均价开仓基差 -0.2469% 低于 0.6%","at":"{blocked_at}"}}}}',
        encoding="utf-8",
    )
    runtime = _runtime(tmp_path, cash_executor=CashCarryExecutor(state))

    scan = runtime._apply_cash_carry_open_scope(
        CashCarryScan(opportunities=[_cash_opportunity(ExchangeName.GATE, "ABCUSDT", "3")])
    )

    assert scan.opportunities == []
    assert len(scan.candidates) == 1
    assert "最近执行深度失败" in " / ".join(scan.candidates[0].blocked_reasons)


def test_mt4_scan_uses_independent_slot(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    assert runtime._full_scan_slots.acquire(blocking=False) is True

    try:
        result, completed = runtime._run_guarded_scan(runtime._mt4_scan_slots, lambda: "mt4-ok", "fallback")
    finally:
        runtime._full_scan_slots.release()

    assert completed is True
    assert result == "mt4-ok"


def _runtime(tmp_path, cash_executor=None, ticker_cache=None) -> LiveRuntimeCache:
    return LiveRuntimeCache(
        _LiveRead(),
        _Scanner(),
        _Mt4Scanner(),
        cash_carry_executor=cash_executor or CashCarryExecutor(tmp_path / "cash.json"),
        ticker_cache=ticker_cache,
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


class _TickerCache:
    max_symbols_per_stream = CASH_CARRY_INTERNAL_CANDIDATE_LIMIT

    def __init__(self) -> None:
        self.subscriptions = {}

    def replace_subscriptions(self, exchange, market_type, subscriptions):
        self.subscriptions[(exchange, market_type)] = dict(subscriptions)
