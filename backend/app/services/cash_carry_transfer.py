from decimal import Decimal

from app.services.live_read import decimal_from
from app.services.reverse_execution_models import ExecutionStep


def transfer_usdt_to_spot(exchange, amount: Decimal, step: ExecutionStep, enabled: bool = True) -> None:
    if not enabled:
        step.status = "skipped"
        step.detail += "；自动划转关闭"
        return
    spot_free = _spot_usdt_free(exchange)
    if spot_free is not None and spot_free >= amount:
        step.status = "skipped"
        step.detail += "；现货 USDT 可用余额充足，无需划转"
        return
    if getattr(exchange, "id", "") == "gateio":
        available = spot_free if spot_free is not None else Decimal("0")
        raise ValueError(f"GATE 现货 USDT 可用余额不足，现货买入需 {amount}，当前可用 {available}")
    from_account = "swap" if getattr(exchange, "id", "") == "bitget" else "funding"
    step.raw = {"spot": exchange.transfer("USDT", float(amount), from_account, "spot")}
    step.status = "done"


def _spot_usdt_free(exchange) -> Decimal | None:
    try:
        balance = exchange.fetch_balance({"type": "spot"})
    except Exception:
        return None
    usdt = balance.get("USDT", {}) if isinstance(balance, dict) else {}
    return decimal_from(usdt.get("free"))
