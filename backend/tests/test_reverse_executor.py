from datetime import datetime, timezone
from decimal import Decimal

from app.core.models import BotSettings, CashCarryOpportunity, DataSource, ExchangeName
from app.services.borrow_pool_blocklist import active_borrow_pool_block, mark_borrow_pool_block
from app.services.reverse_cash_carry_executor import ReverseCashCarryExecutor


def test_reverse_executor_blocks_real_calls_when_manual_confirm_is_required(tmp_path) -> None:
    executor = ReverseCashCarryExecutor(tmp_path / "state.json")
    settings = BotSettings(
        manual_confirm_required=True,
        reverse_cash_carry_auto_open_enabled=True,
        reverse_cash_carry_auto_borrow_enabled=True,
    )

    result = executor.evaluate_open([_opportunity()], settings)

    assert result is not None
    assert result.status == "blocked_by_safety_gate"
    assert "参数要求人工确认" in result.reason
    assert [step.name for step in result.steps] == [
        "transfer_collateral",
        "set_perp_leverage",
        "borrow_spot",
        "sell_borrowed_spot",
        "open_perp_long",
    ]


def test_reverse_executor_does_not_open_when_strategy_switch_is_off(tmp_path) -> None:
    executor = ReverseCashCarryExecutor(tmp_path / "state.json")

    assert executor.evaluate_open([_opportunity()], BotSettings()) is None


def test_reverse_executor_respects_global_open_lock(tmp_path) -> None:
    executor = ReverseCashCarryExecutor(tmp_path / "state.json")
    settings = BotSettings(manual_confirm_required=False, reverse_cash_carry_auto_open_enabled=True)

    assert executor.evaluate_open([_opportunity()], settings, allow_open=False) is None


def test_reverse_executor_skips_bybit_unified_same_account_transfer(tmp_path) -> None:
    executor = _RecordingReverseExecutor(tmp_path / "state.json")
    settings = BotSettings(
        manual_confirm_required=False,
        reverse_cash_carry_auto_open_enabled=True,
        reverse_cash_carry_auto_transfer_enabled=True,
        reverse_cash_carry_auto_borrow_enabled=True,
    )

    result = executor.evaluate_open([_opportunity(ExchangeName.BYBIT)], settings)

    assert result is not None
    assert result.status == "open_submitted"
    assert result.steps[0].status == "skipped"
    assert "统一账户无需重复划转" in result.steps[0].detail


def test_reverse_executor_skips_borrow_pool_blocked_top_opportunity(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.services.borrow_pool_blocklist.DEFAULT_PATH", tmp_path / "borrow_pool_blocks.json")
    mark_borrow_pool_block(ExchangeName.BYBIT, "ABCUSDT", "Borrowing demand is high")
    executor = _RecordingReverseExecutor(tmp_path / "state.json")
    settings = BotSettings(
        manual_confirm_required=False,
        reverse_cash_carry_auto_open_enabled=True,
        reverse_cash_carry_auto_transfer_enabled=True,
        reverse_cash_carry_auto_borrow_enabled=True,
    )

    result = executor.evaluate_open([
        _opportunity(ExchangeName.BYBIT, "ABCUSDT", net="10"),
        _opportunity(ExchangeName.BITGET, "XYZUSDT", net="1"),
    ], settings)

    assert result is not None
    assert result.status == "open_submitted"
    assert result.position is not None
    assert result.position.exchange == ExchangeName.BITGET
    assert result.position.symbol == "XYZUSDT"


def test_reverse_executor_uses_bitget_cross_margin_transfer(tmp_path) -> None:
    executor = _BitgetTransferExecutor(tmp_path / "state.json")
    settings = BotSettings(
        manual_confirm_required=False,
        reverse_cash_carry_auto_open_enabled=True,
        reverse_cash_carry_auto_transfer_enabled=True,
        reverse_cash_carry_auto_borrow_enabled=True,
    )

    result = executor.evaluate_open([_opportunity(ExchangeName.BITGET, "SENTUSDT")], settings)

    assert result is not None
    assert result.status == "open_submitted"
    assert executor.spot.transfers
    args, _kwargs = executor.spot.transfers[0]
    assert args[0] == "USDT"
    assert args[2] == "spot"
    assert args[3] == "cross"
    assert args[4] == {}


def test_reverse_executor_tops_up_bitget_spot_before_cross_margin_transfer(tmp_path) -> None:
    executor = _BitgetTransferExecutor(tmp_path / "state.json", spot_free=Decimal("96"), cross_free=Decimal("0"))
    settings = BotSettings(
        order_notional_usdt=Decimal("100"),
        manual_confirm_required=False,
        reverse_cash_carry_auto_open_enabled=True,
        reverse_cash_carry_auto_transfer_enabled=True,
        reverse_cash_carry_auto_borrow_enabled=True,
    )

    result = executor.evaluate_open([_opportunity(ExchangeName.BITGET, "SENTUSDT")], settings)

    assert result is not None
    assert result.status == "open_submitted"
    assert [item[0] for item in executor.spot.transfers] == [
        ("USDT", 4.0, "swap", "spot"),
        ("USDT", 100.0, "spot", "cross", {}),
    ]


def test_reverse_executor_skips_bitget_transfer_when_cross_margin_has_collateral(tmp_path) -> None:
    executor = _BitgetTransferExecutor(tmp_path / "state.json", spot_free=Decimal("0"), cross_free=Decimal("800"))
    settings = BotSettings(
        order_notional_usdt=Decimal("100"),
        manual_confirm_required=False,
        reverse_cash_carry_auto_open_enabled=True,
        reverse_cash_carry_auto_transfer_enabled=True,
        reverse_cash_carry_auto_borrow_enabled=True,
    )

    result = executor.evaluate_open([_opportunity(ExchangeName.BITGET, "SENTUSDT")], settings)

    assert result is not None
    assert result.status == "open_submitted"
    assert executor.spot.transfers == []


def test_reverse_executor_marks_borrow_failure_as_available_zero(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.services.borrow_pool_blocklist.DEFAULT_PATH", tmp_path / "borrow_pool_blocks.json")
    executor = _BorrowFailExecutor(tmp_path / "state.json")
    settings = BotSettings(
        manual_confirm_required=False,
        reverse_cash_carry_auto_open_enabled=True,
        reverse_cash_carry_auto_borrow_enabled=True,
    )

    result = executor.evaluate_open([_opportunity(ExchangeName.BITGET, "SENTUSDT")], settings)

    assert result is not None
    assert result.status == "failed"
    assert result.steps[2].status == "failed"
    assert executor.spot.orders == []
    assert executor.swap.orders == []
    block = active_borrow_pool_block(ExchangeName.BITGET, "SENTUSDT")
    assert block is not None
    assert block.available_qty == 0


def test_reverse_executor_blocks_before_borrow_when_leverage_mismatch(tmp_path) -> None:
    executor = _LeverageMismatchExecutor(tmp_path / "state.json")
    settings = BotSettings(
        manual_confirm_required=False,
        reverse_cash_carry_auto_open_enabled=True,
        reverse_cash_carry_auto_borrow_enabled=True,
    )

    result = executor.evaluate_open([_opportunity(ExchangeName.BITGET, "SENTUSDT")], settings)

    assert result is not None
    assert result.status == "failed"
    assert result.steps[1].status == "failed"
    assert executor.spot.orders == []
    assert executor.spot.borrowed == []
    assert executor.swap.orders == []


def _opportunity(exchange: ExchangeName = ExchangeName.BITGET, symbol: str = "ABCUSDT", net: str = "0.8") -> CashCarryOpportunity:
    return CashCarryOpportunity(
        exchange=exchange,
        symbol=symbol,
        spot_price=Decimal("100"),
        perp_price=Decimal("99"),
        basis_pct=Decimal("1"),
        funding_rate_pct=Decimal("-0.01"),
        quantity=Decimal("1"),
        spot_volume_24h_usdt=Decimal("1000000"),
        perp_volume_24h_usdt=Decimal("1000000"),
        estimated_basis_profit=Decimal("1"),
        estimated_funding_income=Decimal("0.01"),
        estimated_open_close_fee=Decimal("0.2"),
        estimated_borrow_cost=Decimal("0.01"),
        estimated_net_profit=Decimal(net),
        borrow_check_status="ok",
        borrow_available_qty=Decimal("10"),
        borrow_daily_rate_pct=Decimal("0.01"),
        blocked_reasons=[],
        data_source=DataSource.LIVE,
        updated_at=datetime.now(timezone.utc),
    )


class _RecordingReverseExecutor(ReverseCashCarryExecutor):
    def __init__(self, state_path) -> None:
        super().__init__(state_path)
        self.spot = _FakeSpot()
        self.swap = _FakeSwap()

    def _exchange(self, exchange_name, default_type):
        return self.spot if default_type == "spot" else self.swap

    def _safety_gate(self, settings):
        return []


class _BitgetTransferExecutor(_RecordingReverseExecutor):
    def __init__(self, state_path, spot_free: Decimal = Decimal("200"), cross_free: Decimal = Decimal("0")) -> None:
        super().__init__(state_path)
        self.spot = _FakeBitgetSpot(spot_free, cross_free)


class _BorrowFailExecutor(_RecordingReverseExecutor):
    def __init__(self, state_path) -> None:
        super().__init__(state_path)
        self.spot = _BorrowFailSpot()


class _LeverageMismatchExecutor(_RecordingReverseExecutor):
    def __init__(self, state_path) -> None:
        super().__init__(state_path)
        self.swap = _MismatchLeverageSwap()


class _FakeSpot:
    def __init__(self) -> None:
        self.orders = []
        self.borrowed = []

    def transfer(self, *args, **kwargs):
        raise ValueError('bybit {"retMsg":"server err : fromAccount can not be toAccount"}')

    def borrow_cross_margin(self, code, qty):
        self.borrowed.append((code, qty))
        return {"borrowed": qty, "code": code}

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        self.orders.append({"symbol": symbol, "side": side, "amount": amount})
        return {"id": "spot-1", "symbol": symbol, "side": side, "amount": amount}


class _BorrowFailSpot(_FakeSpot):
    def borrow_cross_margin(self, code, qty):
        raise ValueError("max borrowable amount is 0")


class _FakeBitgetSpot(_FakeSpot):
    id = "bitget"

    def __init__(self, spot_free: Decimal, cross_free: Decimal) -> None:
        super().__init__()
        self.spot_free = spot_free
        self.cross_free = cross_free
        self.transfers = []

    def fetch_balance(self, params):
        return {"USDT": {"free": str(self.spot_free)}}

    def privateMarginGetV2MarginCrossedAccountAssets(self, params):
        return {"data": [{"coin": "USDT", "available": str(self.cross_free)}]}

    def transfer(self, *args, **kwargs):
        self.transfers.append((args, kwargs))
        return {"id": "transfer-1"}


class _FakeSwap:
    def __init__(self) -> None:
        self.orders = []

    def set_leverage(self, leverage, symbol, params=None):
        return {"leverage": leverage, "symbol": symbol, "params": params or {}}

    def load_markets(self):
        return None

    def market(self, symbol):
        return {"contractSize": "1"}

    def amount_to_precision(self, symbol, amount):
        return str(int(amount))

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        self.orders.append({"symbol": symbol, "side": side, "amount": amount})
        return {"id": "swap-1", "symbol": symbol, "side": side, "amount": amount}


class _MismatchLeverageSwap(_FakeSwap):
    id = "bitget"

    def fetch_leverage(self, symbol):
        return {"symbol": symbol, "longLeverage": Decimal("10"), "shortLeverage": Decimal("10")}
