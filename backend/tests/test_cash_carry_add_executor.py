import json
from datetime import datetime, timezone
from decimal import Decimal

from app.core.models import BotSettings, CashCarryOpportunity, CashCarryPositionRow, DataSource, ExchangeName
from app.services.cash_carry_executor import CashCarryExecutor


def test_cash_carry_add_waits_until_basis_widens_from_entry(tmp_path) -> None:
    state = _state_with_open_position(tmp_path, add_count=0)
    executor = _RecordingExecutor(state)
    settings = _settings()

    result = executor.evaluate([_opportunity(basis="3.1")], settings, [_position_row(basis="3.1")], allow_open=False, allow_add=True)

    assert result is None
    assert json.loads(state.read_text(encoding="utf-8"))["positions"][0]["add_count"] == 0


def test_cash_carry_add_submits_spot_buy_and_perp_short_when_basis_widens(tmp_path) -> None:
    state = _state_with_open_position(tmp_path, add_count=0)
    executor = _RecordingExecutor(state)
    settings = _settings()

    result = executor.evaluate([_opportunity(basis="3.3", perp="103.3")], settings, [_position_row(basis="3.3")], allow_open=False, allow_add=True)

    saved = json.loads(state.read_text(encoding="utf-8"))["positions"][0]
    assert result is not None
    assert result.status == "add_submitted"
    assert executor.spot.orders[0]["side"] == "buy"
    assert executor.swap.orders[0]["side"] == "sell"
    assert executor.swap.orders[0]["params"]["reduceOnly"] is False
    assert Decimal(saved["quantity"]) == Decimal("2")
    assert saved["add_count"] == 1
    assert saved["last_add_basis_pct"] == "3.3"
    assert saved["add_orders"][0]["spot_order_id"] == "spot-add"
    assert saved["add_orders"][0]["perp_order_id"] == "perp-add"


def test_cash_carry_add_passes_cross_margin_to_leverage_and_order(tmp_path) -> None:
    state = _state_with_open_position(tmp_path, add_count=0)
    executor = _RecordingExecutor(state)
    settings = _settings(margin_mode="cross")

    result = executor.evaluate([_opportunity(basis="3.3", perp="103.3")], settings, [_position_row(basis="3.3")], allow_open=False, allow_add=True)

    assert result is not None
    assert result.status == "add_submitted"
    assert executor.swap.leverage_calls[0]["params"] == {"marginMode": "cross"}
    assert executor.swap.orders[0]["params"]["marginMode"] == "cross"


def test_cash_carry_add_ignores_same_position_open_scope_reason(tmp_path) -> None:
    state = _state_with_open_position(tmp_path, add_count=0)
    executor = _RecordingExecutor(state)
    settings = _settings()
    opportunity = _opportunity(basis="3.3", perp="103.3")
    opportunity = opportunity.model_copy(update={"blocked_reasons": ["该交易所该币种已有正向期现持仓，禁止重复开仓"]})

    result = executor.evaluate([opportunity], settings, [_position_row(basis="3.3")], allow_open=False, allow_add=True)

    assert result is not None
    assert result.status == "add_submitted"
    assert json.loads(state.read_text(encoding="utf-8"))["positions"][0]["add_count"] == 1


def test_cash_carry_add_uses_independent_add_notional(tmp_path) -> None:
    state = _state_with_open_position(tmp_path, add_count=0)
    executor = _RecordingExecutor(state)
    settings = _settings(add_notional=Decimal("50"))

    result = executor.evaluate([_opportunity(basis="3.3", perp="103.3")], settings, [_position_row(basis="3.3")], allow_open=False, allow_add=True)

    saved = json.loads(state.read_text(encoding="utf-8"))["positions"][0]
    assert result is not None
    assert Decimal(executor.spot.orders[0]["cost"]) == Decimal("50")
    assert Decimal(saved["quantity"]) == Decimal("1.5")
    assert Decimal(saved["add_orders"][0]["quantity"]) == Decimal("0.5")


def test_cash_carry_add_still_blocks_real_market_reason(tmp_path) -> None:
    state = _state_with_open_position(tmp_path, add_count=0)
    executor = _RecordingExecutor(state)
    settings = _settings()
    opportunity = _opportunity(basis="3.3", perp="103.3")
    opportunity = opportunity.model_copy(update={"blocked_reasons": ["盘口深度不足"]})

    result = executor.evaluate([opportunity], settings, [_position_row(basis="3.3")], allow_open=False, allow_add=True)

    assert result is None
    assert executor.spot.orders == []


def test_cash_carry_add_stops_at_max_add_count(tmp_path) -> None:
    state = _state_with_open_position(tmp_path, add_count=2, last_add_basis="3.3")
    executor = _RecordingExecutor(state)
    settings = _settings(max_add_count=2)

    result = executor.evaluate([_opportunity(basis="6")], settings, [_position_row(basis="6")], allow_open=False, allow_add=True)

    assert result is None
    assert executor.spot.orders == []
    assert json.loads(state.read_text(encoding="utf-8"))["positions"][0]["add_count"] == 2


def _settings(max_add_count: int = 4, add_notional: Decimal = Decimal("100"), margin_mode: str = "isolated") -> BotSettings:
    return BotSettings(
        manual_confirm_required=False,
        cash_carry_auto_open_enabled=True,
        cash_carry_auto_trade_enabled=True,
        margin_mode=margin_mode,
        order_notional_usdt=Decimal("100"),
        add_notional_usdt=add_notional,
        add_trigger_spread_pct=Decimal("2.2"),
        max_add_count=max_add_count,
        max_symbol_notional_usdt=Decimal("500"),
        single_exchange_max_notional_usdt=Decimal("500"),
        max_total_notional_usdt=Decimal("2000"),
    )


def _opportunity(basis: str, perp: str = "103.1") -> CashCarryOpportunity:
    return CashCarryOpportunity(
        exchange=ExchangeName.GATE,
        symbol="ABCUSDT",
        spot_price=Decimal("100"),
        perp_price=Decimal(perp),
        basis_pct=Decimal(basis),
        funding_rate_pct=Decimal("0.01"),
        quantity=Decimal("1"),
        spot_volume_24h_usdt=Decimal("1000000"),
        perp_volume_24h_usdt=Decimal("1000000"),
        estimated_basis_profit=Decimal("3"),
        estimated_funding_income=Decimal("0.01"),
        estimated_open_close_fee=Decimal("0.2"),
        estimated_net_profit=Decimal("2.81"),
        blocked_reasons=[],
        data_source=DataSource.LIVE,
        updated_at=datetime.now(timezone.utc),
    )


def _position_row(basis: str) -> CashCarryPositionRow:
    return CashCarryPositionRow(
        exchange=ExchangeName.GATE,
        symbol="ABCUSDT",
        status="matched",
        spot_quantity=Decimal("1"),
        spot_entry_price=Decimal("100"),
        spot_price=Decimal("100"),
        spot_unrealized_pnl=Decimal("0"),
        perp_side="short",
        perp_contracts=Decimal("1"),
        perp_base_quantity=Decimal("1"),
        contract_size=Decimal("1"),
        perp_entry_price=Decimal("101"),
        perp_mark_price=Decimal("103"),
        leverage=Decimal("2"),
        perp_unrealized_pnl=Decimal("-2"),
        estimated_funding_rate_pct=Decimal("0.01"),
        estimated_funding_income=Decimal("0.01"),
        estimated_open_fee=Decimal("0.1"),
        estimated_close_fee=Decimal("0.1"),
        current_net_profit=Decimal("-1.9"),
        quantity_gap=Decimal("0"),
        basis_pct=Decimal(basis),
        updated_at=datetime.now(timezone.utc),
    )


def _state_with_open_position(tmp_path, add_count: int | None = None, last_add_basis: str | None = None):
    item = {
        "id": "pos-1",
        "exchange": "GATE",
        "symbol": "ABCUSDT",
        "base_asset": "ABC",
        "quantity": "1",
        "spot_entry_price": "100",
        "perp_entry_price": "101",
        "spot_order_id": "spot-open",
        "perp_order_id": "perp-open",
        "opened_at": "2026-06-09T00:00:00+00:00",
        "status": "open",
    }
    if add_count is not None:
        item["add_count"] = add_count
    if last_add_basis is not None:
        item["last_add_basis_pct"] = last_add_basis
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"positions": [item]}), encoding="utf-8")
    return state


class _RecordingExecutor(CashCarryExecutor):
    def __init__(self, state_path) -> None:
        super().__init__(state_path)
        self.spot = _RecordingSpot()
        self.swap = _RecordingSwap()

    def _exchange(self, exchange_name, default_type):
        return self.spot if default_type == "spot" else self.swap

    def _safety_gate(self, settings, opening):
        return []


class _RecordingSpot:
    id = "fake"
    has = {"fetchOrder": False}

    def __init__(self) -> None:
        self.orders = []

    def create_market_buy_order_with_cost(self, symbol, cost, params=None):
        filled = Decimal(str(cost)) / Decimal("100")
        order = {"id": "spot-add", "symbol": symbol, "side": "buy", "cost": str(cost), "average": "100", "filled": str(filled)}
        self.orders.append(order)
        return order


class _RecordingSwap:
    id = "gateio"

    def __init__(self) -> None:
        self.orders = []
        self.leverage_calls = []

    def load_markets(self):
        return None

    def market(self, symbol):
        return {"contractSize": "1"}

    def amount_to_precision(self, symbol, amount):
        return str(int(amount))

    def set_leverage(self, leverage, symbol, params=None):
        call = {"leverage": leverage, "symbol": symbol, "params": params or {}}
        self.leverage_calls.append(call)
        return call

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        order = {"id": "perp-add", "symbol": symbol, "side": side, "amount": amount, "average": "103.3", "params": params}
        self.orders.append(order)
        return order
