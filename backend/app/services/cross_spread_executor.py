import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from app.core.env import ENV_PATH, env_bool
from app.core.models import BotSettings, ExchangeName, Opportunity
from app.services.cross_spread_execution_models import CrossSpreadPosition
from app.services.cross_spread_state import CrossSpreadStateStore
from app.services.exchange_factory import build_ccxt_exchange, sanitize_exchange_error
from app.services.live_market_types import SWAP_EXCHANGE_IDS
from app.services.order_sizing import contract_order_amount
from app.services.reverse_execution_models import ExecutionResult, ExecutionStep


class CrossSpreadExecutor:
    def __init__(self, state_path: Path | None = None) -> None:
        root = Path(__file__).resolve().parents[3]
        self.state_path = state_path or root / "config" / "cross_spread_execution_state.json"
        self.state = CrossSpreadStateStore(self.state_path)

    def evaluate(self, rows: list[Opportunity], settings: BotSettings, allow_open: bool = True) -> ExecutionResult | None:
        if settings.emergency_close_enabled:
            return None
        close_result = self.evaluate_close(rows, settings)
        if close_result:
            return close_result
        if not allow_open:
            return None
        return self.evaluate_open(rows, settings)

    def evaluate_open(self, rows: list[Opportunity], settings: BotSettings, allow_open: bool = True) -> ExecutionResult | None:
        if not allow_open or not settings.auto_open_enabled:
            return None
        active_keys = self._active_keys()
        ready = [
            item
            for item in rows
            if item.spot_transfer_ok
            and item.depth_ok
            and item.estimated_funding_net >= settings.min_funding_net_usdt
            and self._row_key(item) not in active_keys
        ]
        if not ready:
            return None
        item = max(ready, key=lambda row: row.estimated_net_profit)
        steps = self._open_plan(item, settings)
        gate_reasons = self._safety_gate(settings)
        if gate_reasons:
            return self.state.remember(ExecutionResult(str(uuid.uuid4()), "blocked_by_safety_gate", " / ".join(gate_reasons), steps))
        return self._execute_open(item, settings, steps)

    def evaluate_close(self, rows: list[Opportunity], settings: BotSettings) -> ExecutionResult | None:
        if not settings.auto_close_enabled:
            return None
        by_key = {(row.symbol, ExchangeName(row.long_exchange), ExchangeName(row.short_exchange)): row for row in rows}
        for record in self.state.load_positions():
            current = by_key.get((record.symbol, record.long_exchange, record.short_exchange))
            if not current or current.spread_pct > settings.target_close_spread_pct:
                continue
            steps = self._close_plan(record, current.spread_pct)
            gate_reasons = self._safety_gate(settings)
            if gate_reasons:
                return self.state.remember(ExecutionResult(str(uuid.uuid4()), "blocked_by_safety_gate", " / ".join(gate_reasons), steps))
            return self._execute_close(record, steps, f"价差收敛到 {current.spread_pct}%")
        return None

    def _execute_open(self, item: Opportunity, settings: BotSettings, steps: list[ExecutionStep]) -> ExecutionResult:
        long_name = ExchangeName(item.long_exchange)
        short_name = ExchangeName(item.short_exchange)
        long_exchange = self._exchange(long_name)
        short_exchange = self._exchange(short_name)
        symbol = self._swap_symbol(item.symbol)
        qty = settings.order_notional_usdt / item.long_price
        long_order_id = None
        short_order_id = None
        try:
            self._run(steps[0], lambda: self._set_leverage(long_exchange, symbol, settings.default_leverage), True)
            self._run(steps[1], lambda: self._set_leverage(short_exchange, symbol, settings.default_leverage), True)
            long_amount = contract_order_amount(long_exchange, symbol, qty)
            short_amount = contract_order_amount(short_exchange, symbol, qty)
            long_order = self._run(steps[2], lambda: long_exchange.create_order(symbol, "market", "buy", long_amount, None, {"reduceOnly": False, "marginMode": settings.margin_mode}), True)
            long_order_id = self._order_id(long_order)
            short_order = self._run(steps[3], lambda: short_exchange.create_order(symbol, "market", "sell", short_amount, None, {"reduceOnly": False, "marginMode": settings.margin_mode}), True)
            short_order_id = self._order_id(short_order)
            position = CrossSpreadPosition(str(uuid.uuid4()), item.symbol, long_name, short_name, qty, item.long_price, item.short_price, long_order_id, short_order_id, datetime.now(timezone.utc))
            self.state.save_position(position)
            return self.state.remember(ExecutionResult(position.id, "open_submitted", "已提交跨平台永续价差开仓流程", steps))
        except Exception as exc:  # noqa: BLE001
            return self.state.remember(ExecutionResult(str(uuid.uuid4()), "failed", self._sanitize(str(exc)), steps))

    def _execute_close(self, record: CrossSpreadPosition, steps: list[ExecutionStep], reason: str) -> ExecutionResult:
        long_exchange = self._exchange(record.long_exchange)
        short_exchange = self._exchange(record.short_exchange)
        symbol = self._swap_symbol(record.symbol)
        try:
            long_amount = contract_order_amount(long_exchange, symbol, record.quantity)
            short_amount = contract_order_amount(short_exchange, symbol, record.quantity)
            self._run(steps[0], lambda: long_exchange.create_order(symbol, "market", "sell", long_amount, None, {"reduceOnly": True}), True)
            self._run(steps[1], lambda: short_exchange.create_order(symbol, "market", "buy", short_amount, None, {"reduceOnly": True}), True)
            self.state.mark_closed(record.id, reason)
            return self.state.remember(ExecutionResult(record.id, "close_submitted", f"已提交跨平台永续价差平仓流程：{reason}", steps))
        except Exception as exc:  # noqa: BLE001
            return self.state.remember(ExecutionResult(record.id, "failed", self._sanitize(str(exc)), steps))

    def _open_plan(self, item: Opportunity, settings: BotSettings) -> list[ExecutionStep]:
        qty = settings.order_notional_usdt / item.long_price
        return [
            ExecutionStep("set_long_leverage", "pending", f"{item.long_exchange} 设置杠杆 {settings.default_leverage}x"),
            ExecutionStep("set_short_leverage", "pending", f"{item.short_exchange} 设置杠杆 {settings.default_leverage}x"),
            ExecutionStep("open_long", "pending", f"{item.long_exchange} 做多 {item.symbol}，数量约 {qty}"),
            ExecutionStep("open_short", "pending", f"{item.short_exchange} 做空 {item.symbol}，数量约 {qty}"),
        ]

    def _close_plan(self, record: CrossSpreadPosition, spread_pct: Decimal) -> list[ExecutionStep]:
        return [
            ExecutionStep("close_long", "pending", f"{record.long_exchange} 平多 {record.symbol}，价差 {spread_pct}%"),
            ExecutionStep("close_short", "pending", f"{record.short_exchange} 平空 {record.symbol}，价差 {spread_pct}%"),
        ]

    def _run(self, step: ExecutionStep, action, enabled: bool):
        if not enabled:
            step.status = "skipped"
            return None
        result = action()
        step.status = "done"
        step.raw = result if isinstance(result, dict) else {"result": str(result)}
        return result

    def _exchange(self, exchange_name: ExchangeName):
        return build_ccxt_exchange(exchange_name, SWAP_EXCHANGE_IDS[exchange_name], "swap", timeout=12000)

    def _set_leverage(self, exchange, symbol: str, leverage: Decimal):
        return exchange.set_leverage(float(leverage), symbol) if hasattr(exchange, "set_leverage") else {"skipped": True}

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

    def has_active_records(self) -> bool:
        return self.state.has_active_records()

    def _active_keys(self) -> set[tuple[str, ExchangeName, ExchangeName]]:
        return {(item.symbol, item.long_exchange, item.short_exchange) for item in self.state.load_positions()}

    def _row_key(self, item: Opportunity) -> tuple[str, ExchangeName, ExchangeName]:
        return (item.symbol, ExchangeName(item.long_exchange), ExchangeName(item.short_exchange))

    def _swap_symbol(self, symbol: str) -> str:
        return f"{symbol.removesuffix('USDT')}/USDT:USDT"

    def _order_id(self, order) -> str | None:
        return order.get("id") if isinstance(order, dict) else None

    def _sanitize(self, message: str) -> str:
        return sanitize_exchange_error(message)[:220]
