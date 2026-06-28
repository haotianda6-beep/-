from datetime import datetime, timezone
from decimal import Decimal

from app.core.models import BotSettings, CashCarryOpportunity, DataSource, ExchangeName
from app.services.cash_carry_execution_models import CASH_CARRY_RULESET_VERSION
from app.services.cash_carry_executor import CashCarryExecutor


def test_cash_carry_open_records_actual_fill_prices(tmp_path) -> None:
    executor = _OpeningExecutor(tmp_path / "state.json")
    settings = BotSettings(manual_confirm_required=False, cash_carry_auto_open_enabled=True)
    opportunity = _opportunity()

    result = executor._execute_open(opportunity, settings, executor._open_plan(opportunity, settings))

    assert result.status == "open_submitted"
    state = executor.state.read()["positions"][0]
    assert state["spot_entry_price"] == "100.25"
    assert state["perp_entry_price"] == "101.75"
    assert state["strategy_version"] == CASH_CARRY_RULESET_VERSION
    assert state["entry_basis_pct"] == "1.4963"
    assert state["entry_estimated_net_profit"] == "1.0645"
    assert state["entry_estimated_funding_income"] == "0.0100"
    assert state["entry_estimated_open_close_fee"] == "0.2418"
    assert state["entry_notional_usdt"] == "100.00"


def test_cash_carry_open_still_records_position_when_fee_lookup_fails(tmp_path) -> None:
    executor = _FeeFailOpeningExecutor(tmp_path / "state.json")
    settings = BotSettings(manual_confirm_required=False, cash_carry_auto_open_enabled=True)

    result = executor._execute_open(_opportunity(), settings, executor._open_plan(_opportunity(), settings))

    assert result.status == "open_submitted"
    state = executor.state.read()["positions"][0]
    assert state["spot_order_id"] == "spot-open"
    assert state["perp_order_id"] == "perp-open"
    assert state["entry_estimated_open_close_fee"] == "0.2418"


def test_cash_carry_open_rolls_back_spot_when_second_leg_quality_slips(tmp_path) -> None:
    executor = _PostSpotSlipExecutor(tmp_path / "state.json")
    settings = BotSettings(
        manual_confirm_required=False,
        cash_carry_auto_open_enabled=True,
        cash_carry_min_basis_pct=Decimal("0.8"),
        cash_carry_bootstrap_enabled=False,
    )

    result = executor._execute_open(_opportunity(), settings, executor._open_plan(_opportunity(), settings))

    assert result.status == "blocked_by_depth"
    assert "已自动卖出现货回滚" in result.reason
    assert executor.state.read().get("positions", []) == []
    assert executor.spot.sell_orders == [("ABC/USDT", "market", "sell")]
    assert executor.swap.created_orders == []


def _opportunity() -> CashCarryOpportunity:
    return CashCarryOpportunity(
        exchange=ExchangeName.GATE,
        symbol="ABCUSDT",
        spot_price=Decimal("100"),
        perp_price=Decimal("101.75"),
        basis_pct=Decimal("1.75"),
        funding_rate_pct=Decimal("0.01"),
        quantity=Decimal("1"),
        spot_volume_24h_usdt=Decimal("1000000"),
        perp_volume_24h_usdt=Decimal("1000000"),
        estimated_basis_profit=Decimal("1.55"),
        estimated_funding_income=Decimal("0.01"),
        estimated_open_close_fee=Decimal("0.2"),
        estimated_net_profit=Decimal("1.36"),
        blocked_reasons=[],
        data_source=DataSource.LIVE,
        updated_at=datetime.now(timezone.utc),
    )


class _OpeningSpot:
    has = {"fetchOrder": True}
    id = "gateio"

    def fetch_balance(self, params):
        return {"USDT": {"free": "1000"}}

    def fetch_order_book(self, symbol, limit=20):
        return {"asks": [[100.25, 10]], "bids": [[100, 10]]}

    def create_market_buy_order_with_cost(self, symbol, cost, params=None):
        return {"id": "spot-open"}

    def fetch_order(self, order_id, symbol):
        return {"id": order_id, "average": "100.25", "cost": "100", "filled": "0.997506"}


class _RollbackSpot(_OpeningSpot):
    def __init__(self) -> None:
        self.sell_orders = []

    def fetch_balance(self, params):
        return {"USDT": {"free": "1000"}, "ABC": {"free": "0.997506"}}

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        self.sell_orders.append((symbol, order_type, side))
        return {"id": "spot-rollback", "average": "100"}


class _OpeningSwap:
    has = {"fetchOrder": True}
    id = "gateio"

    def __init__(self) -> None:
        self.created_orders = []

    def load_markets(self):
        return None

    def market(self, symbol):
        return {"contractSize": "1"}

    def amount_to_precision(self, symbol, amount):
        return str(amount)

    def fetch_order_book(self, symbol, limit=20):
        return {"bids": [[101.75, 10]], "asks": [[102, 10]]}

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        self.created_orders.append((symbol, order_type, side, amount, params))
        return {"id": "perp-open", "params": params}

    def fetch_order(self, order_id, symbol):
        return {"id": order_id, "average": "101.75", "amount": "0.997506"}

    def set_leverage(self, leverage, symbol, params=None):
        return {"leverage": leverage, "symbol": symbol, "params": params or {}}


class _OpeningExecutor(CashCarryExecutor):
    def __init__(self, state_path) -> None:
        super().__init__(state_path)
        self.spot = _OpeningSpot()
        self.swap = _OpeningSwap()

    def _exchange(self, exchange_name, default_type):
        return self.spot if default_type == "spot" else self.swap


class _FeeFailOpeningExecutor(_OpeningExecutor):
    def _taker_fee(self, exchange, market_type, symbol):
        raise RuntimeError("fee lookup failed")


class _PostSpotSlipSwap(_OpeningSwap):
    def __init__(self) -> None:
        super().__init__()
        self.book_calls = 0

    def fetch_order_book(self, symbol, limit=20):
        self.book_calls += 1
        if self.book_calls == 1:
            return {"bids": [[101.75, 10]], "asks": [[102, 10]]}
        return {"bids": [[100.3, 10]], "asks": [[102, 10]]}


class _PostSpotSlipExecutor(CashCarryExecutor):
    def __init__(self, state_path) -> None:
        super().__init__(state_path)
        self.spot = _RollbackSpot()
        self.swap = _PostSpotSlipSwap()

    def _exchange(self, exchange_name, default_type):
        return self.spot if default_type == "spot" else self.swap
