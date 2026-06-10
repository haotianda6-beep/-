from datetime import datetime, timezone
from decimal import Decimal

from app.core.models import BotSettings, DataSource, ExchangeName, Opportunity
from app.services.cross_spread_executor import CrossSpreadExecutor


def test_cross_spread_executor_blocks_when_manual_confirm_is_required(tmp_path) -> None:
    executor = CrossSpreadExecutor(tmp_path / "state.json")
    settings = BotSettings(auto_open_enabled=True, manual_confirm_required=True)

    result = executor.evaluate_open([_opportunity()], settings)

    assert result is not None
    assert result.status == "blocked_by_safety_gate"
    assert "参数要求人工确认" in result.reason
    assert [step.name for step in result.steps] == ["set_long_leverage", "set_short_leverage", "open_long", "open_short"]


def test_cross_spread_executor_respects_global_open_lock(tmp_path) -> None:
    executor = CrossSpreadExecutor(tmp_path / "state.json")
    settings = BotSettings(auto_open_enabled=True, manual_confirm_required=False)

    assert executor.evaluate_open([_opportunity()], settings, allow_open=False) is None


def test_cross_spread_executor_does_not_open_when_switch_is_off(tmp_path) -> None:
    executor = CrossSpreadExecutor(tmp_path / "state.json")

    assert executor.evaluate_open([_opportunity()], BotSettings(auto_open_enabled=False)) is None


def test_cross_spread_executor_submits_balanced_long_and_short(tmp_path) -> None:
    executor = _RecordingExecutor(tmp_path / "state.json")
    settings = BotSettings(auto_open_enabled=True, manual_confirm_required=False, order_notional_usdt=Decimal("100"))

    result = executor.evaluate_open([_opportunity()], settings)

    assert result is not None
    assert result.status == "open_submitted"
    assert executor.long.orders[0]["side"] == "buy"
    assert executor.short.orders[0]["side"] == "sell"
    assert executor.long.orders[0]["amount"] == 10000.0
    assert executor.short.orders[0]["amount"] == 10000.0


def test_cross_spread_executor_allows_new_pair_when_same_strategy_has_active_record(tmp_path) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        '{"positions":[{"id":"1","symbol":"ABCUSDT","long_exchange":"BINANCE","short_exchange":"OKX","quantity":"1","long_entry_price":"100","short_entry_price":"102","opened_at":"2026-06-09T00:00:00+00:00","status":"open"}]}',
        encoding="utf-8",
    )
    executor = _RecordingExecutor(state)
    settings = BotSettings(auto_open_enabled=True, manual_confirm_required=False, order_notional_usdt=Decimal("100"))

    result = executor.evaluate_open([_opportunity("ABCUSDT"), _opportunity("XYZUSDT")], settings)

    assert result is not None
    assert result.status == "open_submitted"
    assert "XYZUSDT" in result.steps[2].detail


def _opportunity(symbol: str = "ABCUSDT") -> Opportunity:
    return Opportunity(
        symbol=symbol,
        long_exchange=ExchangeName.BINANCE,
        short_exchange=ExchangeName.OKX,
        long_price=Decimal("100"),
        short_price=Decimal("102"),
        spread_pct=Decimal("2"),
        long_volume_24h_usdt=Decimal("1000000"),
        short_volume_24h_usdt=Decimal("1000000"),
        min_volume_24h_usdt=Decimal("1000000"),
        estimated_open_close_fee=Decimal("0.2"),
        estimated_funding_net=Decimal("0.2"),
        estimated_net_profit=Decimal("1.8"),
        spot_transfer_ok=True,
        depth_ok=True,
        risk_tags=[],
        data_source=DataSource.LIVE,
        updated_at=datetime.now(timezone.utc),
    )


class _FakeSwap:
    def __init__(self) -> None:
        self.orders = []

    def set_leverage(self, leverage, symbol):
        return {"leverage": leverage, "symbol": symbol}

    def load_markets(self):
        return None

    def market(self, symbol):
        return {"contractSize": "0.0001", "info": {"is_pre_market": False}}

    def amount_to_precision(self, symbol, amount):
        return str(int(amount))

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        order = {"symbol": symbol, "side": side, "amount": amount, "params": params}
        self.orders.append(order)
        return {"id": f"{side}-1", **order}


class _RecordingExecutor(CrossSpreadExecutor):
    def __init__(self, state_path) -> None:
        super().__init__(state_path)
        self.long = _FakeSwap()
        self.short = _FakeSwap()

    def _exchange(self, exchange_name):
        return self.long if exchange_name == ExchangeName.BINANCE else self.short

    def _safety_gate(self, settings):
        return []
