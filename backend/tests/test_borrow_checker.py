from decimal import Decimal

from app.core.models import ExchangeName
from app.services.borrow_checker import BorrowCheck, BorrowChecker


def test_bybit_available_falls_back_to_collateral_info_when_max_loan_is_zero() -> None:
    amount = BorrowChecker()._bybit_available(_FakeBybit(), "H")

    assert amount == Decimal("100000")


def test_okx_inst_id_error_is_classified_as_not_margin_borrowable() -> None:
    reason = BorrowChecker()._borrow_error_reason(
        ExchangeName.OKX,
        "SAHARA",
        'okx {"code":"51000","data":[],"msg":"Parameter instIdSAHARA-USDT error"}',
    )

    assert "不是现货杠杆可借交易对" in reason
    assert "接口读取失败" not in reason


def test_bitget_available_uses_cross_max_borrowable_amount() -> None:
    amount = BorrowChecker()._fetch_available_qty(_FakeBitget(), ExchangeName.BITGET, "SENT", BorrowCheck())

    assert amount == Decimal("0.0006")


def test_bitget_max_borrow_error_is_classified_as_unavailable() -> None:
    reason = BorrowChecker()._borrow_error_reason(
        ExchangeName.BITGET,
        "SENT",
        'bitget {"code":"50035","msg":"The maximum borrowing amount is exceeded"}',
    )

    assert "实际最大可借数量不足" in reason
    assert "接口读取失败" not in reason


def test_fetch_keeps_rate_when_available_endpoint_fails(monkeypatch) -> None:
    monkeypatch.setattr("app.services.borrow_checker.credential_statuses", lambda: [])
    result = _PartialBorrowChecker()._fetch(ExchangeName.OKX, "SAHARA")

    assert result.daily_rate == Decimal("0.0002")
    assert "不是现货杠杆可借交易对" in " / ".join(result.blocked_reasons)
    assert "可借数量未确认" not in " / ".join(result.blocked_reasons)


class _FakeBybit:
    def privateGetV5SpotMarginTradeMaxBorrowable(self, params):
        return {"result": {"maxLoan": "0"}}

    def privateGetV5AccountCollateralInfo(self, params):
        return {"result": {"list": [{"currency": params["currency"], "borrowable": True, "availableToBorrow": "100000"}]}}


class _FakeBitget:
    def currency(self, code):
        return {"id": code}

    def privateMarginGetV2MarginCrossedAccountMaxBorrowableAmount(self, params):
        assert params == {"coin": "SENT"}
        return {"data": {"maxBorrowableAmount": "0.0006", "coin": "SENT"}}


class _PartialBorrowChecker(BorrowChecker):
    def _build_exchange(self, exchange_name):
        return object()

    def _fetch_rate(self, exchange, exchange_name, code):
        return BorrowCheck(daily_rate=Decimal("0.0002"), rate_period_hours=Decimal("24"))

    def _fetch_available_qty(self, exchange, exchange_name, code, rate_info):
        raise ValueError('okx {"code":"51000","data":[],"msg":"Parameter instIdSAHARA-USDT error"}')
