import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from app.core.env import ENV_PATH, env_bool
from app.core.models import BotSettings, CashCarryOpportunity, ExchangeName
from app.services.borrow_pool_blocklist import active_borrow_pool_reason, is_rate_limit_error, mark_borrow_pool_block
from app.services.exchange_factory import build_ccxt_exchange, sanitize_exchange_error
from app.services.live_market_types import SPOT_EXCHANGE_IDS, SWAP_EXCHANGE_IDS
from app.services.live_read import decimal_from
from app.services.order_sizing import contract_order_amount
from app.services.reverse_execution_models import ExecutionResult, ExecutionStep, ReversePositionRecord


class ReverseCashCarryExecutor:
    def __init__(self, state_path: Path | None = None) -> None:
        root = Path(__file__).resolve().parents[3]
        self.state_path = state_path or root / "config" / "reverse_execution_state.json"

    def evaluate(self, scan_rows: list[CashCarryOpportunity], settings: BotSettings, allow_open: bool = True, allowed_open_exchanges: set[ExchangeName] | None = None) -> ExecutionResult | None:
        if settings.emergency_close_enabled:
            return None
        close_result = self.evaluate_close(scan_rows, settings)
        if close_result:
            return close_result
        if not allow_open:
            return None
        return self.evaluate_open(scan_rows, settings, allowed_open_exchanges=allowed_open_exchanges)

    def evaluate_open(self, opportunities: list[CashCarryOpportunity], settings: BotSettings, allow_open: bool = True, allowed_open_exchanges: set[ExchangeName] | None = None) -> ExecutionResult | None:
        if not allow_open:
            return None
        if not settings.reverse_cash_carry_auto_open_enabled:
            return None
        ready = [
            item
            for item in opportunities
            if self._is_ready_to_open(item, allowed_open_exchanges)
        ]
        if not ready:
            return None
        item = max(ready, key=lambda row: row.estimated_net_profit)
        if (ExchangeName(item.exchange), item.symbol) in self._active_keys():
            return None
        steps = self._open_plan(item, settings)
        gate_reasons = self._safety_gate(settings)
        if gate_reasons:
            return self._remember(ExecutionResult(str(uuid.uuid4()), "blocked_by_safety_gate", " / ".join(gate_reasons), steps))
        return self._execute_open(item, settings, steps)

    def _is_ready_to_open(self, item: CashCarryOpportunity, allowed_open_exchanges: set[ExchangeName] | None) -> bool:
        exchange = ExchangeName(item.exchange)
        return (
            not item.blocked_reasons
            and item.borrow_check_status == "ok"
            and (allowed_open_exchanges is None or exchange in allowed_open_exchanges)
            and active_borrow_pool_reason(exchange, item.symbol) is None
        )

    def evaluate_close(self, rows: list[CashCarryOpportunity], settings: BotSettings) -> ExecutionResult | None:
        if not settings.reverse_cash_carry_auto_close_enabled:
            return None
        active = self._load_positions()
        if not active:
            return None
        by_key = {(row.exchange, row.symbol): row for row in rows}
        for record in active:
            current = by_key.get((record.exchange, record.symbol))
            if current and current.basis_pct <= settings.reverse_cash_carry_close_discount_pct:
                steps = self._close_plan(record, settings, current.basis_pct)
                gate_reasons = self._safety_gate(settings)
                if gate_reasons:
                    return self._remember(ExecutionResult(str(uuid.uuid4()), "blocked_by_safety_gate", " / ".join(gate_reasons), steps))
                return self._execute_close(record, settings, steps)
        return None

    def _execute_open(self, item: CashCarryOpportunity, settings: BotSettings, steps: list[ExecutionStep]) -> ExecutionResult:
        spot = self._exchange(item.exchange, "spot")
        swap = self._exchange(item.exchange, "swap")
        base = self._base(item.symbol)
        spot_symbol = f"{base}/USDT"
        swap_symbol = f"{base}/USDT:USDT"
        borrow_quantity = self._execution_quantity(spot, spot_symbol, item.quantity)
        qty = float(borrow_quantity)
        spot_order_id = None
        perp_order_id = None
        try:
            self._maybe_transfer(spot, item, settings, steps[0])
            try:
                self._run(steps[1], lambda: self._set_leverage(swap, swap_symbol, settings.default_leverage, settings.margin_mode), True)
                self._verify_leverage(swap, swap_symbol, settings.default_leverage, "long", settings.margin_mode, steps[1])
            except Exception as exc:  # noqa: BLE001
                reason = self._sanitize(str(exc))
                steps[1].status = "failed"
                steps[1].raw = {"error": reason}
                return self._remember(ExecutionResult(str(uuid.uuid4()), "failed", reason, steps))
            try:
                borrow_quantity = self._borrow_spot_with_retry(spot, base, borrow_quantity, settings, steps[2])
                qty = float(borrow_quantity)
            except Exception as exc:  # noqa: BLE001
                reason = self._sanitize(str(exc))
                mark_borrow_pool_block(item.exchange, item.symbol, reason, seconds=60 if is_rate_limit_error(reason) else 900)
                return self._remember(ExecutionResult(str(uuid.uuid4()), "failed", reason, steps))
            spot_order = self._run(
                steps[3],
                lambda: spot.create_order(spot_symbol, "market", "sell", qty, None, {"marginMode": "cross"}),
                True,
            )
            spot_order_id = self._order_id(spot_order)
            contract_qty = contract_order_amount(swap, swap_symbol, borrow_quantity)
            perp_order = self._run(
                steps[4],
                lambda: swap.create_order(swap_symbol, "market", "buy", contract_qty, None, {"reduceOnly": False, "marginMode": settings.margin_mode}),
                True,
            )
            perp_order_id = self._order_id(perp_order)
            position = ReversePositionRecord(
                id=str(uuid.uuid4()),
                exchange=item.exchange,
                symbol=item.symbol,
                base_asset=base,
                quantity=borrow_quantity,
                borrowed_quantity=borrow_quantity,
                spot_entry_price=item.spot_price,
                perp_entry_price=item.perp_price,
                spot_order_id=spot_order_id,
                perp_order_id=perp_order_id,
                opened_at=datetime.now(timezone.utc),
            )
            self._save_position(position)
            return self._remember(ExecutionResult(position.id, "open_submitted", "已提交借币反向期现开仓流程", steps, position))
        except Exception as exc:  # noqa: BLE001
            reason = self._sanitize(str(exc))
            return self._remember(ExecutionResult(str(uuid.uuid4()), "failed", reason, steps))

    def _execute_close(
        self,
        record: ReversePositionRecord,
        settings: BotSettings,
        steps: list[ExecutionStep],
    ) -> ExecutionResult:
        spot = self._exchange(record.exchange, "spot")
        swap = self._exchange(record.exchange, "swap")
        spot_symbol = f"{record.base_asset}/USDT"
        swap_symbol = f"{record.base_asset}/USDT:USDT"
        qty = float(record.quantity)
        repay_qty = float(record.borrowed_quantity * (Decimal("1") + settings.reverse_cash_carry_repay_buffer_pct / Decimal("100")))
        try:
            contract_qty = contract_order_amount(swap, swap_symbol, record.quantity)
            self._run(steps[0], lambda: swap.create_order(swap_symbol, "market", "sell", contract_qty, None, {"reduceOnly": True}), True)
            self._run(steps[1], lambda: spot.create_order(spot_symbol, "market", "buy", repay_qty, None, {"marginMode": "cross"}), True)
            self._run(steps[2], lambda: spot.repay_cross_margin(record.base_asset, repay_qty), settings.reverse_cash_carry_auto_repay_enabled)
            self._mark_closed(record.id)
            return self._remember(ExecutionResult(record.id, "close_submitted", "已提交平仓和还款流程", steps, record))
        except Exception as exc:  # noqa: BLE001
            return self._remember(ExecutionResult(record.id, "failed", self._sanitize(str(exc)), steps, record))

    def _open_plan(self, item: CashCarryOpportunity, settings: BotSettings) -> list[ExecutionStep]:
        return [
            ExecutionStep("transfer_collateral", "pending", f"按需划转 USDT 到现货杠杆/合约账户，名义本金 {item.estimated_net_profit} 净利预估"),
            ExecutionStep("set_perp_leverage", "pending", f"设置合约杠杆 {settings.default_leverage}x"),
            ExecutionStep("borrow_spot", "pending", f"借入 {item.quantity} {self._base(item.symbol)}"),
            ExecutionStep("sell_borrowed_spot", "pending", f"市价卖出现货 {item.symbol}，数量 {item.quantity}"),
            ExecutionStep("open_perp_long", "pending", f"市价开合约多单 {item.symbol}，数量 {item.quantity}"),
        ]

    def _close_plan(self, record: ReversePositionRecord, settings: BotSettings, basis_pct: Decimal) -> list[ExecutionStep]:
        repay_qty = record.borrowed_quantity * (Decimal("1") + settings.reverse_cash_carry_repay_buffer_pct / Decimal("100"))
        return [
            ExecutionStep("close_perp_long", "pending", f"折价收敛到 {basis_pct}%，市价平合约多单"),
            ExecutionStep("buy_spot_back", "pending", f"买回现货 {repay_qty} {record.base_asset}，含还款缓冲"),
            ExecutionStep("repay_borrowed_spot", "pending", f"自动还款 {repay_qty} {record.base_asset}"),
        ]

    def _maybe_transfer(self, exchange, item: CashCarryOpportunity, settings: BotSettings, step: ExecutionStep) -> None:
        if not settings.reverse_cash_carry_auto_transfer_enabled:
            step.status = "skipped"
            step.detail += "；自动划转关闭"
            return
        amount = settings.order_notional_usdt
        is_bitget = getattr(exchange, "id", "") == "bitget"
        to_account = "cross" if is_bitget else "margin"
        params = {} if is_bitget else {"symbol": self._spot_symbol(item.symbol)}
        raw = {}
        try:
            if is_bitget:
                cross_free = self._bitget_cross_usdt_available(exchange)
                if cross_free is not None and cross_free >= amount:
                    step.raw = {"skipped": f"Bitget cross USDT available {cross_free} >= {amount}"}
                    step.status = "skipped"
                    step.detail += "；跨保证金 USDT 可用余额充足，无需划转"
                    return
                required = amount - (cross_free or Decimal("0"))
                spot_free = self._spot_usdt_free(exchange)
                if spot_free is not None and spot_free < required:
                    raw["spot_top_up"] = exchange.transfer("USDT", float(required - spot_free), "swap", "spot")
                raw["margin"] = exchange.transfer("USDT", float(required), "spot", to_account, params)
            else:
                raw["margin"] = exchange.transfer("USDT", float(amount), "spot", to_account, params)
            step.raw = raw
        except Exception as exc:  # noqa: BLE001
            if "fromAccount can not be toAccount" not in str(exc):
                raise
            step.raw = {"skipped": self._sanitize(str(exc))}
            step.status = "skipped"
            step.detail += "；统一账户无需重复划转"
            return
        step.status = "done"

    def _spot_usdt_free(self, exchange) -> Decimal | None:
        try:
            balance = exchange.fetch_balance({"type": "spot"})
        except Exception:
            return None
        usdt = balance.get("USDT", {}) if isinstance(balance, dict) else {}
        return decimal_from(usdt.get("free"))

    def _bitget_cross_usdt_available(self, exchange) -> Decimal | None:
        try:
            response = exchange.privateMarginGetV2MarginCrossedAccountAssets({})
        except Exception:
            return None
        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, list):
            return None
        for item in data:
            if isinstance(item, dict) and item.get("coin") == "USDT":
                return decimal_from(item.get("available"))
        return Decimal("0")

    def _run(self, step: ExecutionStep, action, enabled: bool):
        if not enabled:
            step.status = "skipped"
            step.detail += "；对应自动开关关闭"
            return None
        result = action()
        step.status = "done"
        step.raw = result if isinstance(result, dict) else {"result": str(result)}
        return result

    def _execution_quantity(self, exchange, symbol: str, quantity: Decimal) -> Decimal:
        if not hasattr(exchange, "amount_to_precision"):
            return quantity
        try:
            return Decimal(str(exchange.amount_to_precision(symbol, float(quantity))))
        except Exception:
            return quantity

    def _borrow_spot_with_retry(self, spot, base: str, quantity: Decimal, settings: BotSettings, step: ExecutionStep) -> Decimal:
        if not settings.reverse_cash_carry_auto_borrow_enabled:
            step.status = "skipped"
            step.detail += "；对应自动开关关闭"
            return quantity
        try:
            step.raw = {"quantity": str(quantity), "result": spot.borrow_cross_margin(base, float(quantity))}
            step.status = "done"
            return quantity
        except Exception as exc:  # noqa: BLE001
            reason = self._sanitize(str(exc))
            retry_quantity = self._borrow_precision_retry_quantity(quantity, reason)
            if retry_quantity is None:
                step.status = "failed"
                step.raw = {"quantity": str(quantity), "error": reason}
                raise ValueError(reason)
        try:
            result = spot.borrow_cross_margin(base, float(retry_quantity))
        except Exception as exc:  # noqa: BLE001
            retry_reason = self._sanitize(str(exc))
            step.status = "failed"
            step.raw = {"quantity": str(quantity), "initial_error": reason, "retry_quantity": str(retry_quantity), "error": retry_reason}
            raise ValueError(retry_reason)
        step.status = "done"
        step.detail += f"；借币精度调整为 {retry_quantity}"
        step.raw = {"quantity": str(quantity), "initial_error": reason, "adjusted_quantity": str(retry_quantity), "result": result}
        return retry_quantity

    def _borrow_precision_retry_quantity(self, quantity: Decimal, reason: str) -> Decimal | None:
        text = reason.lower()
        if "precision" not in text and "integer multiple" not in text:
            return None
        retry = quantity.to_integral_value(rounding=ROUND_DOWN)
        if retry <= 0 or retry == quantity:
            return None
        return retry

    def _set_leverage(self, exchange, symbol: str, leverage: Decimal, margin_mode: str | None = None):
        if not hasattr(exchange, "set_leverage"):
            return {"skipped": True}
        exchange_id = getattr(exchange, "id", "")
        margin_result = self._set_margin_mode(exchange, symbol, margin_mode, leverage)
        if exchange_id == "bitget" and margin_mode == "isolated":
            return {
                "margin_mode": margin_result,
                "long": self._set_leverage_once(exchange, leverage, symbol, {"holdSide": "long"}),
                "short": self._set_leverage_once(exchange, leverage, symbol, {"holdSide": "short"}),
            }
        return {
            "margin_mode": margin_result,
            "leverage": self._set_leverage_once(exchange, leverage, symbol, self._leverage_params(exchange_id, margin_mode)),
        }

    def _set_leverage_once(self, exchange, leverage: Decimal, symbol: str, params: dict[str, Any]):
        try:
            return exchange.set_leverage(float(leverage), symbol, params)
        except Exception as exc:  # noqa: BLE001
            if self._already_set_error(exc):
                return {"skipped": "already_set", "message": self._sanitize(str(exc))}
            raise

    def _set_margin_mode(self, exchange, symbol: str, margin_mode: str | None, leverage: Decimal):
        if not margin_mode or not hasattr(exchange, "set_margin_mode"):
            return {"skipped": True}
        exchange_id = getattr(exchange, "id", "")
        if exchange_id in {"okx", "gateio", "bitget"}:
            return {"skipped": True}
        params = {"leverage": str(leverage)} if exchange_id == "bybit" else {}
        try:
            return exchange.set_margin_mode(margin_mode, symbol, params)
        except Exception as exc:  # noqa: BLE001
            text = str(exc).lower()
            if "no need" in text or "already" in text or "not modified" in text:
                return {"skipped": "already_set"}
            raise

    def _already_set_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return "no need" in text or "already" in text or "not modified" in text

    def _leverage_params(self, exchange_id: str, margin_mode: str | None) -> dict[str, Any]:
        if exchange_id == "okx":
            params = {"marginMode": margin_mode or "cross"}
            if margin_mode == "isolated":
                params["posSide"] = "net"
            return params
        if exchange_id == "gateio" and margin_mode:
            return {"marginMode": margin_mode}
        return {}

    def _verify_leverage(self, exchange, symbol: str, expected: Decimal, side: str, margin_mode: str | None, step: ExecutionStep) -> None:
        if not hasattr(exchange, "set_leverage"):
            return
        raw = self._fetch_leverage_snapshot(exchange, symbol, margin_mode)
        actual = self._leverage_value(raw, side, margin_mode) or self._leverage_value(step.raw or {}, side, margin_mode)
        if actual is None:
            step.status = "failed"
            step.raw = {"expected": str(expected), "leverage": step.raw, "verification": raw}
            raise ValueError(f"{str(getattr(exchange, 'id', '')).upper()} {symbol} 未能确认实际{side}杠杆，已阻止开仓")
        if actual != expected:
            step.status = "failed"
            step.raw = {"expected": str(expected), "actual": str(actual), "leverage": step.raw, "verification": raw}
            raise ValueError(f"{str(getattr(exchange, 'id', '')).upper()} {symbol} 实际{side}杠杆 {actual}x 与参数 {expected}x 不一致，已阻止开仓")
        if isinstance(step.raw, dict):
            step.raw = {**step.raw, "verified_leverage": str(actual), "verification": raw}

    def _fetch_leverage_snapshot(self, exchange, symbol: str, margin_mode: str | None):
        if not hasattr(exchange, "fetch_leverage"):
            return {}
        try:
            if getattr(exchange, "id", "") == "okx" and margin_mode:
                return exchange.fetch_leverage(symbol, {"marginMode": margin_mode})
            return exchange.fetch_leverage(symbol)
        except Exception:
            return {}

    def _leverage_value(self, raw: Any, side: str, margin_mode: str | None = None) -> Decimal | None:
        keys = ("shortLeverage", "isolatedShortLever") if side == "short" else ("longLeverage", "isolatedLongLever")
        cross_keys = ("crossMarginLeverage", "crossedMarginLeverage", "cross_leverage_limit")
        keys = (*keys, *cross_keys, "leverage") if margin_mode == "cross" else (*keys, "leverage", *cross_keys)
        for key in keys:
            value = self._find_key(raw, key)
            if value not in (None, ""):
                return Decimal(str(value))
        return None

    def _find_key(self, raw: Any, key: str) -> Any:
        if isinstance(raw, dict):
            if key in raw:
                value = raw[key]
                if not isinstance(value, (dict, list)):
                    return value
                found = self._find_key(value, key)
                if found not in (None, ""):
                    return found
            for value in raw.values():
                found = self._find_key(value, key)
                if found not in (None, ""):
                    return found
        if isinstance(raw, list):
            for value in raw:
                found = self._find_key(value, key)
                if found not in (None, ""):
                    return found
        return None

    def _exchange(self, exchange_name: ExchangeName, default_type: str):
        exchange_name = ExchangeName(exchange_name)
        exchange_id = SPOT_EXCHANGE_IDS[exchange_name] if default_type == "spot" else SWAP_EXCHANGE_IDS[exchange_name]
        return build_ccxt_exchange(exchange_name, exchange_id, default_type, timeout=12000)

    def _safety_gate(self, settings: BotSettings) -> list[str]:
        load_dotenv(ENV_PATH, override=False)
        reasons = []
        if not env_bool("TRADING_ENABLED"):
            reasons.append("TRADING_ENABLED 未开启")
        if not env_bool("ORDER_EXECUTION_ENABLED"):
            reasons.append("ORDER_EXECUTION_ENABLED 未开启")
        if env_bool("API_READ_ONLY_MODE", default=True):
            reasons.append("API_READ_ONLY_MODE 仍为只读")
        if settings.manual_confirm_required:
            reasons.append("参数要求人工确认")
        return reasons

    def _load_positions(self) -> list[ReversePositionRecord]:
        raw = self._read_state().get("positions", [])
        result = []
        for item in raw:
            if item.get("status") != "open":
                continue
            result.append(
                ReversePositionRecord(
                    id=item["id"],
                    exchange=ExchangeName(item["exchange"]),
                    symbol=item["symbol"],
                    base_asset=item["base_asset"],
                    quantity=Decimal(item["quantity"]),
                    borrowed_quantity=Decimal(item["borrowed_quantity"]),
                    spot_entry_price=Decimal(item["spot_entry_price"]),
                    perp_entry_price=Decimal(item["perp_entry_price"]),
                    spot_order_id=item.get("spot_order_id"),
                    perp_order_id=item.get("perp_order_id"),
                    opened_at=datetime.fromisoformat(item["opened_at"]),
                    status=item.get("status", "open"),
                )
            )
        return result

    def _save_position(self, position: ReversePositionRecord) -> None:
        state = self._read_state()
        positions = state.setdefault("positions", [])
        positions.append(self._position_dict(position))
        self._write_state(state)

    def _mark_closed(self, position_id: str) -> None:
        state = self._read_state()
        for item in state.get("positions", []):
            if item.get("id") == position_id:
                item["status"] = "closed"
                item["closed_at"] = datetime.now(timezone.utc).isoformat()
        self._write_state(state)

    def _remember(self, result: ExecutionResult) -> ExecutionResult:
        state = self._read_state()
        state["last_result"] = {"id": result.id, "status": result.status, "reason": result.reason, "at": datetime.now(timezone.utc).isoformat()}
        self._write_state(state)
        return result

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"positions": []}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _write_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _active_keys(self) -> set[tuple[ExchangeName, str]]:
        return {(item.exchange, item.symbol) for item in self._load_positions()}
    def active_exchanges(self) -> set[ExchangeName]:
        return {item.exchange for item in self._load_positions()}
    def has_active_records(self) -> bool:
        return bool(self._active_keys())

    def _position_dict(self, item: ReversePositionRecord) -> dict[str, Any]:
        exchange = item.exchange.value if hasattr(item.exchange, "value") else str(item.exchange)
        return {**item.__dict__, "exchange": exchange, "opened_at": item.opened_at.isoformat(), "quantity": str(item.quantity), "borrowed_quantity": str(item.borrowed_quantity), "spot_entry_price": str(item.spot_entry_price), "perp_entry_price": str(item.perp_entry_price)}

    def _spot_symbol(self, symbol: str) -> str:
        return f"{self._base(symbol)}/USDT"

    def _base(self, symbol: str) -> str:
        return symbol.removesuffix("USDT")

    def _order_id(self, order) -> str | None:
        return order.get("id") if isinstance(order, dict) else None

    def _sanitize(self, message: str) -> str:
        return sanitize_exchange_error(message)[:220]
