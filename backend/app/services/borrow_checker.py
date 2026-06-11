from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from dotenv import load_dotenv

from app.core.env import ENV_PATH, credential_statuses
from app.core.models import ExchangeName
from app.services.exchange_factory import build_ccxt_exchange, sanitize_exchange_error
from app.services.live_market_types import SPOT_EXCHANGE_IDS
from app.services.live_read import decimal_from


@dataclass
class BorrowCheck:
    status: str = "unknown"
    available_qty: Decimal | None = None
    daily_rate: Decimal | None = None
    rate_period_hours: Decimal | None = None
    estimated_cost_usdt: Decimal | None = None
    term: str | None = None
    risk_tags: list[str] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)
    raw_info: Any = None


class BorrowChecker:
    def __init__(self) -> None:
        self._cache: dict[tuple[ExchangeName, str], tuple[datetime, BorrowCheck]] = {}

    def clear_caches(self) -> None:
        self._cache = {}

    def check(
        self,
        exchange_name: ExchangeName,
        code: str,
        required_qty: Decimal,
        reference_price: Decimal,
        hold_hours: Decimal,
    ) -> BorrowCheck:
        normalized_code = code.upper()
        cached = self._cache.get((exchange_name, normalized_code))
        now = datetime.now(timezone.utc)
        if cached and (now - cached[0]).total_seconds() < 180:
            return self._with_cost(cached[1], required_qty, reference_price, hold_hours)
        result = self._fetch(exchange_name, normalized_code)
        self._cache[(exchange_name, normalized_code)] = (now, result)
        return self._with_cost(result, required_qty, reference_price, hold_hours)

    def _fetch(self, exchange_name: ExchangeName, code: str) -> BorrowCheck:
        load_dotenv(ENV_PATH, override=False)
        status = next((item for item in credential_statuses() if item.exchange == exchange_name), None)
        if status and not status.configured:
            return BorrowCheck(status="unknown", blocked_reasons=[f"{exchange_name}: API 凭证未配置完整，借币额度未确认"])
        try:
            exchange = self._build_exchange(exchange_name)
            rate_info = self._fetch_rate(exchange, exchange_name, code)
        except Exception as exc:  # noqa: BLE001
            return BorrowCheck(status="unknown", blocked_reasons=[self._borrow_error_reason(exchange_name, code, str(exc))])
        reasons = []
        available_qty = None
        try:
            available_qty = self._fetch_available_qty(exchange, exchange_name, code, rate_info)
        except Exception as exc:  # noqa: BLE001
            reasons.append(self._borrow_error_reason(exchange_name, code, str(exc)))
        if rate_info.daily_rate is None:
            reasons.append(f"{exchange_name}: 借币利率未确认")
        if available_qty is None and not self._reasons_confirm_unavailable(reasons):
            reasons.append(f"{exchange_name}: 可借数量未确认")
        return BorrowCheck(
            status="unknown" if reasons else "ok",
            available_qty=available_qty,
            daily_rate=rate_info.daily_rate,
            rate_period_hours=rate_info.rate_period_hours,
            term="活期借币，通常无固定到期日",
            risk_tags=self._risk_tags(),
            blocked_reasons=reasons,
            raw_info=rate_info.raw_info,
        )

    def _build_exchange(self, exchange_name: ExchangeName):
        exchange_id = SPOT_EXCHANGE_IDS[exchange_name]
        return build_ccxt_exchange(exchange_name, exchange_id, "spot", timeout=12000)

    def _fetch_rate(self, exchange, exchange_name: ExchangeName, code: str) -> BorrowCheck:
        if exchange_name == ExchangeName.BYBIT:
            return self._bybit_rate(exchange, code)
        if exchange.has.get("fetchCrossBorrowRate"):
            raw = exchange.fetch_cross_borrow_rate(code)
            period_ms = decimal_from(raw.get("period"), "86400000")
            rate = decimal_from(raw.get("rate"))
            daily_rate = rate * Decimal("86400000") / period_ms if period_ms > 0 else rate
            return BorrowCheck(daily_rate=daily_rate, rate_period_hours=period_ms / Decimal("3600000"), raw_info=raw.get("info"))
        if exchange_name == ExchangeName.GATE:
            return BorrowCheck(daily_rate=self._gate_rate(exchange, code), rate_period_hours=Decimal("24"))
        return BorrowCheck()

    def _fetch_available_qty(self, exchange, exchange_name: ExchangeName, code: str, rate_info: BorrowCheck) -> Decimal | None:
        if exchange_name == ExchangeName.BINANCE and hasattr(exchange, "sapiGetMarginMaxBorrowable"):
            currency = exchange.currency(code)
            raw = exchange.sapiGetMarginMaxBorrowable({"asset": currency["id"]})
            return self._extract_amount(raw, ("amount", "borrowLimit"))
        if exchange_name == ExchangeName.OKX and hasattr(exchange, "privateGetAccountMaxLoan"):
            currency = exchange.currency(code)
            raw = exchange.privateGetAccountMaxLoan({"instId": f"{currency['id']}-USDT", "mgnMode": "cross", "mgnCcy": currency["id"]})
            return self._extract_amount(raw, ("maxLoan", "maxLoanCcy", "amt"))
        if exchange_name == ExchangeName.BYBIT:
            return self._bybit_available(exchange, code)
        if exchange_name == ExchangeName.GATE:
            return self._gate_available(exchange, code)
        if exchange_name == ExchangeName.BITGET and hasattr(exchange, "privateMarginGetV2MarginCrossedAccountMaxBorrowableAmount"):
            currency = exchange.currency(code)
            raw = exchange.privateMarginGetV2MarginCrossedAccountMaxBorrowableAmount({"coin": currency["id"]})
            return self._extract_amount(raw, ("maxBorrowableAmount", "maxBorrowable", "amount"))
        info = rate_info.raw_info or {}
        return self._extract_amount(info, ("maxBorrowableAmount", "loanAbleAmount", "maxLoanAmount", "limit"))

    def _gate_available(self, exchange, code: str) -> Decimal | None:
        methods = (
            ("privateMarginGetUniBorrowable", {"currency": code}),
            ("privateMarginGetCrossBorrowable", {"currency": code}),
            ("privateUnifiedGetBorrowable", {"currency": code}),
        )
        for method_name, params in methods:
            if hasattr(exchange, method_name):
                try:
                    return self._extract_amount(getattr(exchange, method_name)(params), ("amount", "borrowable", "left_quota", "available"))
                except Exception:
                    continue
        return None

    def _gate_rate(self, exchange, code: str) -> Decimal | None:
        methods = (
            ("privateUnifiedGetEstimateRate", {"currencies": code}),
            ("privateMarginGetUniEstimateRate", {"currencies": code}),
            ("privateMarginGetCrossEstimateRate", {"currencies": code}),
        )
        for method_name, params in methods:
            if hasattr(exchange, method_name):
                try:
                    raw = getattr(exchange, method_name)(params)
                    hourly = decimal_from(raw.get(code), "-1") if isinstance(raw, dict) else Decimal("-1")
                    if hourly >= 0:
                        return hourly * Decimal("24")
                    return self._extract_rate(raw)
                except Exception:
                    continue
        return None

    def _bybit_rate(self, exchange, code: str) -> BorrowCheck:
        if hasattr(exchange, "privateGetV5AccountCollateralInfo"):
            raw = exchange.privateGetV5AccountCollateralInfo({"currency": code})
            hourly = self._extract_rate(raw)
            if hourly is not None:
                return BorrowCheck(daily_rate=hourly * Decimal("24"), rate_period_hours=Decimal("1"), raw_info=raw)
        if hasattr(exchange, "privateGetV5SpotMarginTradeInterestRateHistory"):
            raw = exchange.privateGetV5SpotMarginTradeInterestRateHistory({"currency": code})
            hourly = self._extract_rate(raw)
            if hourly is not None:
                return BorrowCheck(daily_rate=hourly * Decimal("24"), rate_period_hours=Decimal("1"), raw_info=raw)
        return BorrowCheck()

    def _bybit_available(self, exchange, code: str) -> Decimal | None:
        amount = None
        if hasattr(exchange, "privateGetV5SpotMarginTradeMaxBorrowable"):
            raw = exchange.privateGetV5SpotMarginTradeMaxBorrowable({"currency": code})
            amount = self._extract_amount(raw, ("maxLoan", "availableToBorrow", "maxBorrowingAmount"))
            if amount is not None and amount > 0:
                return amount
        if hasattr(exchange, "privateGetV5AccountCollateralInfo"):
            try:
                raw = exchange.privateGetV5AccountCollateralInfo({"currency": code})
            except Exception:
                return amount
            collateral_amount = self._bybit_collateral_available(raw)
            return collateral_amount if collateral_amount is not None else amount
        return amount

    def _bybit_collateral_available(self, raw: Any) -> Decimal | None:
        item = self._first_mapping(raw)
        borrowable = self._find_key(item, "borrowable")
        if str(borrowable).lower() in {"false", "0"}:
            return Decimal("0")
        return self._extract_amount(raw, ("availableToBorrow", "freeBorrowingAmount", "maxBorrowingAmount"))

    def _with_cost(
        self,
        result: BorrowCheck,
        required_qty: Decimal,
        reference_price: Decimal,
        hold_hours: Decimal,
    ) -> BorrowCheck:
        reasons = list(result.blocked_reasons)
        status = result.status
        if result.available_qty is not None and result.available_qty < required_qty:
            status = "blocked"
            reasons.append(f"可借数量不足，需要 {required_qty}，可借 {result.available_qty}")
        cost = None
        if result.daily_rate is not None:
            cost = required_qty * reference_price * result.daily_rate * hold_hours / Decimal("24")
        return BorrowCheck(
            status=status if not reasons else "blocked",
            available_qty=result.available_qty,
            daily_rate=result.daily_rate,
            rate_period_hours=result.rate_period_hours,
            estimated_cost_usdt=cost,
            term=result.term,
            risk_tags=list(result.risk_tags),
            blocked_reasons=reasons,
            raw_info=result.raw_info,
        )

    def _extract_amount(self, raw: Any, keys: tuple[str, ...]) -> Decimal | None:
        item = self._first_mapping(raw)
        for key in keys:
            value = self._find_key(item, key)
            amount = decimal_from(value, "-1")
            if amount >= 0:
                return amount
        return None

    def _extract_rate(self, raw: Any) -> Decimal | None:
        item = self._first_mapping(raw)
        for key in ("rate", "daily_rate", "dailyRate", "interest_rate", "interestRate", "hourlyBorrowRate"):
            value = self._find_key(item, key)
            rate = decimal_from(value, "-1")
            if rate >= 0:
                return rate
        return None

    def _first_mapping(self, raw: Any) -> Any:
        if isinstance(raw, dict):
            for key in ("data", "result"):
                value = raw.get(key)
                if isinstance(value, list) and value:
                    return value[0]
                if isinstance(value, dict):
                    return value
            return raw
        if isinstance(raw, list) and raw:
            return raw[0]
        return raw

    def _find_key(self, raw: Any, key: str) -> Any:
        if isinstance(raw, list):
            values = raw
        elif isinstance(raw, dict):
            if key in raw:
                return raw[key]
            values = raw.values()
        else:
            return None
        for value in values:
            found = self._find_key(value, key)
            if found is not None:
                return found
        return None

    def _risk_tags(self) -> list[str]:
        return [
            "活期借币利率可能浮动",
            "可借额度会随账户抵押率和平台额度变化",
            "需监控借币召回/强制还款通知",
        ]

    def _sanitize(self, message: str) -> str:
        return sanitize_exchange_error(message)[:220]

    def _borrow_error_reason(self, exchange_name: ExchangeName, code: str, message: str) -> str:
        clean = self._sanitize(message)
        lower = clean.lower()
        if "does not support cross" in lower:
            return f"{exchange_name}: {code} 不支持 cross 借币"
        if "maximum borrowing amount is exceeded" in lower:
            return f"{exchange_name}: {code} 实际最大可借数量不足"
        if exchange_name == ExchangeName.OKX and "parameter instid" in lower and "error" in lower:
            return f"{exchange_name}: {code}-USDT 不是现货杠杆可借交易对或未开放最大借币额度"
        if "margin trading account does not exist" in lower or "margin account does not exist" in lower:
            return f"{exchange_name}: 保证金账户不存在或未开通"
        return f"{exchange_name}: 借币接口读取失败 {clean}"

    def _reasons_confirm_unavailable(self, reasons: list[str]) -> bool:
        text = " / ".join(reasons)
        return "不支持 cross 借币" in text or "不是现货杠杆可借交易对" in text
