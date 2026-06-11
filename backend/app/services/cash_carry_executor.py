import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from dotenv import load_dotenv
from app.core.env import ENV_PATH, env_bool
from app.core.market_math import FEE_RATES
from app.core.models import BotSettings, CashCarryOpportunity, CashCarryPositionRow, ExchangeName
from app.services.cash_carry_add_executor import evaluate_cash_carry_add
from app.services.cash_carry_close_policy import cash_carry_close_decision
from app.services.cash_carry_execution_guard import forward_close_depth_guard, forward_open_depth_guard
from app.services.cash_carry_execution_models import CashCarryPosition
from app.services.cash_carry_reconciler import build_cash_carry_external_perp_close_history, build_cash_carry_history
from app.services.cash_carry_state import CashCarryStateStore
from app.services.cash_carry_transfer import transfer_usdt_to_spot
from app.services.exchange_factory import build_ccxt_exchange, sanitize_exchange_error
from app.services.live_market_types import SPOT_EXCHANGE_IDS, SWAP_EXCHANGE_IDS
from app.services.order_sizing import contract_order_amount, fetch_order_snapshot, filled_base_quantity, order_average_price, spot_market_buy
from app.services.reverse_execution_models import ExecutionResult, ExecutionStep

class CashCarryExecutor:
    reopen_cooldown_seconds = 3600
    def __init__(self, state_path: Path | None = None) -> None:
        root = Path(__file__).resolve().parents[3]
        self.state_path = state_path or root / "config" / "cash_carry_execution_state.json"
        self.state = CashCarryStateStore(self.state_path)

    def evaluate(
        self,
        rows: list[CashCarryOpportunity],
        settings: BotSettings,
        position_rows: list[CashCarryPositionRow] | None = None,
        allow_open: bool = True,
        allow_add: bool = False,
        allowed_open_exchanges: set[ExchangeName] | None = None,
    ) -> ExecutionResult | None:
        if settings.emergency_close_enabled:
            return None
        close_result = self.evaluate_close(rows, settings, position_rows)
        if close_result:
            return close_result
        if allow_add:
            add_result = evaluate_cash_carry_add(self, rows, settings, position_rows)
            if add_result:
                return add_result
        if not allow_open:
            return None
        return self.evaluate_open(rows, settings, allowed_open_exchanges=allowed_open_exchanges)

    def evaluate_open(self, rows: list[CashCarryOpportunity], settings: BotSettings, allow_open: bool = True, allowed_open_exchanges: set[ExchangeName] | None = None) -> ExecutionResult | None:
        if not allow_open or not settings.cash_carry_auto_open_enabled:
            return None
        blocked_keys = self.state.active_keys() | self.state.recently_closed_keys(self.reopen_cooldown_seconds)
        ready = [item for item in rows if not item.blocked_reasons and (item.exchange, item.symbol) not in blocked_keys and (allowed_open_exchanges is None or ExchangeName(item.exchange) in allowed_open_exchanges)]
        if not ready:
            return None
        item = max(ready, key=lambda row: row.estimated_net_profit)
        steps = self._open_plan(item, settings)
        gate_reasons = self._safety_gate(settings, opening=True)
        if gate_reasons:
            return self.state.remember(ExecutionResult(str(uuid.uuid4()), "blocked_by_safety_gate", " / ".join(gate_reasons), steps))
        return self._execute_open(item, settings, steps)

    def evaluate_close(
        self,
        rows: list[CashCarryOpportunity],
        settings: BotSettings,
        position_rows: list[CashCarryPositionRow] | None = None,
    ) -> ExecutionResult | None:
        if not settings.cash_carry_auto_close_enabled or not position_rows:
            return None
        live_by_key = {(ExchangeName(row.exchange), row.symbol): row for row in position_rows or []}
        for record in self.state.load_positions(include_non_open=True):
            live = live_by_key.get((record.exchange, record.symbol))
            if not live:
                if record.status == "open":
                    reason, extra = self._missing_live_perp_status(record)
                    self.state.mark_status(record.id, "mismatch", reason, extra)
                    return self.state.remember(ExecutionResult(record.id, "failed", reason, []))
                continue
            if record.status == "mismatch" and live.status == "matched":
                self.state.mark_status(record.id, "open")
            decision = cash_carry_close_decision(live.current_net_profit, live.basis_pct, live.estimated_funding_rate_pct, settings, has_live_net=True)
            if not decision.should_close or not self._live_close_safe(live):
                continue
            steps = self._close_plan(record, live.basis_pct, decision.reason, live.spot_quantity)
            gate_reasons = self._safety_gate(settings, opening=False)
            if gate_reasons:
                return self.state.remember(ExecutionResult(str(uuid.uuid4()), "blocked_by_safety_gate", " / ".join(gate_reasons), steps))
            return self._execute_close(record, steps, decision.reason, settings, live.spot_quantity, live.perp_base_quantity)
        return None

    def _execute_open(self, item: CashCarryOpportunity, settings: BotSettings, steps: list[ExecutionStep]) -> ExecutionResult:
        spot = self._exchange(item.exchange, "spot")
        swap = self._exchange(item.exchange, "swap")
        base = self._base(item.symbol)
        spot_symbol = f"{base}/USDT"
        swap_symbol = f"{base}/USDT:USDT"
        base_qty = item.quantity
        spot_order_id = None
        perp_order_id = None
        spot_entry_price = item.spot_price
        try:
            guard = forward_open_depth_guard(spot, swap, spot_symbol, swap_symbol, settings.order_notional_usdt, settings.cash_carry_min_basis_pct)
            if not guard.ok:
                return self.state.remember(ExecutionResult(str(uuid.uuid4()), "blocked_by_depth", guard.reason, steps))
            self._maybe_transfer(spot, item, settings, steps[0])
            self._run(steps[1], lambda: self._set_leverage(swap, swap_symbol, settings.default_leverage, settings.margin_mode), True)
            self._verify_leverage(swap, swap_symbol, settings.default_leverage, "short", steps[1])
            spot_order_raw = self._run(steps[2], lambda: spot_market_buy(spot, spot_symbol, settings.order_notional_usdt, item.quantity), True)
            spot_order = fetch_order_snapshot(spot, spot_symbol, spot_order_raw)
            base_qty = filled_base_quantity(spot, spot_symbol, spot_order, item.quantity)
            spot_entry_price = order_average_price(spot_order, item.spot_price)
            spot_order_id = self._order_id(spot_order)
            contract_qty = contract_order_amount(swap, swap_symbol, base_qty)
            perp_order_raw = self._run(
                steps[3],
                lambda: swap.create_order(swap_symbol, "market", "sell", contract_qty, None, {"reduceOnly": False, "marginMode": settings.margin_mode}),
                True,
            )
            perp_order = fetch_order_snapshot(swap, swap_symbol, perp_order_raw)
            perp_order_id = self._order_id(perp_order)
            perp_entry_price = order_average_price(perp_order, item.perp_price)
            position = CashCarryPosition(
                id=str(uuid.uuid4()),
                exchange=item.exchange,
                symbol=item.symbol,
                base_asset=base,
                quantity=base_qty,
                spot_entry_price=spot_entry_price,
                perp_entry_price=perp_entry_price,
                spot_order_id=spot_order_id,
                perp_order_id=perp_order_id,
                opened_at=datetime.now(timezone.utc),
            )
            self.state.save_position(position)
            return self.state.remember(ExecutionResult(position.id, "open_submitted", "已提交正向期现开仓流程", steps))
        except Exception as exc:  # noqa: BLE001
            if spot_order_id and not perp_order_id:
                position = CashCarryPosition(
                    id=str(uuid.uuid4()),
                    exchange=item.exchange,
                    symbol=item.symbol,
                    base_asset=base,
                    quantity=base_qty,
                    spot_entry_price=spot_entry_price,
                    perp_entry_price=item.perp_price,
                    spot_order_id=spot_order_id,
                    perp_order_id=None,
                    opened_at=datetime.now(timezone.utc),
                    status="spot_only",
                )
                self.state.save_position(position)
            return self.state.remember(ExecutionResult(str(uuid.uuid4()), "failed", self._sanitize(str(exc)), steps))

    def _execute_close(
        self,
        record: CashCarryPosition,
        steps: list[ExecutionStep],
        reason: str = "",
        settings: BotSettings | None = None,
        spot_quantity: Decimal | None = None,
        perp_quantity: Decimal | None = None,
    ) -> ExecutionResult:
        spot = self._exchange(record.exchange, "spot")
        swap = self._exchange(record.exchange, "swap")
        spot_symbol = f"{record.base_asset}/USDT"
        swap_symbol = f"{record.base_asset}/USDT:USDT"
        spot_qty = spot_quantity or record.quantity
        perp_qty = perp_quantity or record.quantity
        try:
            guard = forward_close_depth_guard(
                spot,
                swap,
                spot_symbol,
                swap_symbol,
                spot_qty,
                perp_qty,
                record.spot_entry_price,
                record.perp_entry_price,
                self._fee_rate(record.exchange),
                self._close_profit_floor(settings) if self._requires_profit_floor(reason) else Decimal("-999999999"),
            )
            if not guard.ok:
                return self.state.remember(ExecutionResult(record.id, "blocked_by_depth", guard.reason, steps))
            contract_qty = contract_order_amount(swap, swap_symbol, perp_qty)
            perp_order = self._run(steps[0], lambda: swap.create_order(swap_symbol, "market", "buy", contract_qty, None, {"reduceOnly": True}), True)
            spot_order = self._run(steps[1], lambda: spot.create_order(spot_symbol, "market", "sell", float(spot_qty)), True)
            close_fields = self._close_fields(spot_order, perp_order)
            history = build_cash_carry_history(spot, swap, record, spot_symbol, swap_symbol, close_fields["close_spot_order_id"], close_fields["close_perp_order_id"])
            if history:
                close_fields["history"] = history
            self.state.mark_closed(record.id, reason, close_fields)
            suffix = f"：{reason}" if reason else ""
            return self.state.remember(ExecutionResult(record.id, "close_submitted", f"已提交正向期现平仓流程{suffix}", steps))
        except Exception as exc:  # noqa: BLE001
            return self.state.remember(ExecutionResult(record.id, "failed", self._sanitize(str(exc)), steps))

    def _missing_live_perp_status(self, record: CashCarryPosition) -> tuple[str, dict[str, Any]]:
        reason = f"{record.exchange} {record.symbol} 本地有开仓记录，但实盘合约仓位为空，已标记 mismatch"
        extra: dict[str, Any] = {}
        try:
            spot = self._exchange(record.exchange, "spot")
            swap = self._exchange(record.exchange, "swap")
            spot_symbol = f"{record.base_asset}/USDT"
            swap_symbol = f"{record.base_asset}/USDT:USDT"
            history = build_cash_carry_external_perp_close_history(spot, swap, record, spot_symbol, swap_symbol)
            if not history:
                return reason, extra
            is_liquidation = history.get("external_close_type") == "liquidation"
            action = "交易所强平" if is_liquidation else "外部平仓"
            reason = f"{record.exchange} {record.symbol} 合约腿已被{action}，现货仍持有，已标记 mismatch"
            extra = {
                "history": history,
                "closed_at": history.get("closed_at"),
                "close_perp_order_id": history.get("close_perp_order_id"),
                "perp_close_price": history.get("short_close_price"),
                "spot_close_price": None,
            }
        except Exception as exc:  # noqa: BLE001 - keep live monitor running even if reconciliation fails.
            reason = f"{reason}；强平对账失败 {self._sanitize(str(exc))}"
        return reason, extra

    def _open_plan(self, item: CashCarryOpportunity, settings: BotSettings) -> list[ExecutionStep]:
        return [
            ExecutionStep("transfer_usdt", "pending", f"按需划转 USDT，单笔名义 {settings.order_notional_usdt}"),
            ExecutionStep("set_perp_leverage", "pending", f"设置合约杠杆 {settings.default_leverage}x"),
            ExecutionStep("buy_spot", "pending", f"买入现货 {item.symbol}，数量 {item.quantity}"),
            ExecutionStep("open_perp_short", "pending", f"做空合约 {item.symbol}，数量 {item.quantity}"),
        ]

    def _close_plan(
        self,
        record: CashCarryPosition,
        basis_pct: Decimal,
        reason: str = "",
        spot_quantity: Decimal | None = None,
    ) -> list[ExecutionStep]:
        qty = spot_quantity or record.quantity
        prefix = f"{reason}，" if reason else f"基差收敛到 {basis_pct}%，"
        return [
            ExecutionStep("close_perp_short", "pending", f"{prefix}平合约空单"),
            ExecutionStep("sell_spot", "pending", f"卖出现货 {record.symbol}，数量 {qty}"),
        ]

    def _maybe_transfer(self, exchange, item: CashCarryOpportunity, settings: BotSettings, step: ExecutionStep) -> None:
        transfer_usdt_to_spot(exchange, settings.order_notional_usdt, step, settings.cash_carry_auto_transfer_enabled)

    def _run(self, step: ExecutionStep, action, enabled: bool):
        if not enabled:
            step.status = "skipped"
            step.detail += "；自动下单关闭"
            return None
        result = action()
        step.status = "done"
        step.raw = result if isinstance(result, dict) else {"result": str(result)}
        return result

    def _safety_gate(self, settings: BotSettings, opening: bool) -> list[str]:
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
        if opening and not settings.cash_carry_auto_trade_enabled:
            reasons.append("正向期现自动下单未开启")
        return reasons

    def _exchange(self, exchange_name: ExchangeName, default_type: str):
        exchange_name = ExchangeName(exchange_name)
        exchange_id = SPOT_EXCHANGE_IDS[exchange_name] if default_type == "spot" else SWAP_EXCHANGE_IDS[exchange_name]
        return build_ccxt_exchange(exchange_name, exchange_id, default_type, timeout=12000)

    def _set_leverage(self, exchange, symbol: str, leverage: Decimal, margin_mode: str | None = None):
        if not hasattr(exchange, "set_leverage"):
            return {"skipped": True}
        if getattr(exchange, "id", "") == "bitget" and margin_mode == "isolated":
            return {
                "long": exchange.set_leverage(float(leverage), symbol, {"holdSide": "long"}),
                "short": exchange.set_leverage(float(leverage), symbol, {"holdSide": "short"}),
            }
        return exchange.set_leverage(float(leverage), symbol)

    def _verify_leverage(self, exchange, symbol: str, expected: Decimal, side: str, step: ExecutionStep) -> None:
        if getattr(exchange, "id", "") != "bitget" or not hasattr(exchange, "fetch_leverage"):
            return
        raw = exchange.fetch_leverage(symbol)
        actual = self._leverage_value(raw, side)
        if actual is None:
            return
        if actual != expected:
            step.status = "failed"
            step.raw = {"expected": str(expected), "actual": str(actual), "leverage": raw}
            raise ValueError(f"BITGET {symbol} 实际{side}杠杆 {actual}x 与参数 {expected}x 不一致，已阻止开仓")

    def _leverage_value(self, raw: dict[str, Any], side: str) -> Decimal | None:
        keys = ("shortLeverage", "isolatedShortLever") if side == "short" else ("longLeverage", "isolatedLongLever")
        info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
        for key in keys:
            value = raw.get(key) if key in raw else info.get(key)
            if value not in (None, ""):
                return Decimal(str(value))
        return None

    def has_active_records(self) -> bool: return bool(self.state.active_keys())

    def _live_close_safe(self, row: CashCarryPositionRow) -> bool:
        if row.status != "matched" or row.spot_quantity <= 0 or row.perp_base_quantity <= 0:
            return False
        tolerance = max(Decimal("0.01"), max(abs(row.spot_quantity), abs(row.perp_base_quantity)) * Decimal("0.01"))
        return abs(row.quantity_gap) <= tolerance

    def _base(self, symbol: str) -> str: return symbol.removesuffix("USDT")

    def _order_id(self, order) -> str | None: return order.get("id") if isinstance(order, dict) else None

    def _fee_rate(self, exchange: ExchangeName) -> Decimal:
        return FEE_RATES.get(ExchangeName(exchange), Decimal("0.0006"))

    def _close_profit_floor(self, settings: BotSettings | None) -> Decimal:
        if not settings:
            return Decimal("0.05")
        return max(Decimal("0.05"), settings.order_notional_usdt * settings.max_slippage_pct / Decimal("100"))

    def _requires_profit_floor(self, reason: str) -> bool:
        return "亏损" not in reason and "止损" not in reason

    def _close_fields(self, spot_order, perp_order) -> dict[str, Any]:
        return {
            "close_spot_order_id": self._order_id(spot_order),
            "close_perp_order_id": self._order_id(perp_order),
            "spot_close_price": self._order_price(spot_order),
            "perp_close_price": self._order_price(perp_order),
            "close_spot_raw": spot_order if isinstance(spot_order, dict) else None,
            "close_perp_raw": perp_order if isinstance(perp_order, dict) else None,
        }

    def _order_price(self, order) -> str | None:
        if not isinstance(order, dict):
            return None
        price = order.get("average") or order.get("price")
        return str(price) if price not in (None, "") else None

    def _sanitize(self, message: str) -> str:
        return sanitize_exchange_error(message)[:220]
