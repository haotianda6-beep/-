import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from dotenv import load_dotenv
from app.core.env import ENV_PATH, env_bool
from app.core.market_math import FEE_RATES
from app.core.market_math import q
from app.core.models import BotSettings, CashCarryOpportunity, CashCarryPositionRow, ExchangeName
from app.services.account_fee_rates import account_taker_fee_map, cached_account_taker_fee
from app.services.cash_carry_add_executor import evaluate_cash_carry_add
from app.services.cash_carry_close_policy import CashCarryCloseDecision, cash_carry_close_decision
from app.services.cash_carry_execution_guard import forward_close_depth_guard, forward_open_depth_guard
from app.services.cash_carry_execution_models import CashCarryPosition
from app.services.cash_carry_history_quality import CashCarryHistoryQuality
from app.services.cash_carry_reconciler import build_cash_carry_external_perp_close_history, build_cash_carry_history
from app.services.cash_carry_quality import close_execution_buffer
from app.services.cash_carry_scope import CASH_CARRY_EXCHANGE_SET
from app.services.cash_carry_state import CashCarryStateStore
from app.services.cash_carry_transfer import transfer_usdt_to_spot
from app.services.exchange_factory import build_ccxt_exchange, sanitize_exchange_error
from app.services.live_market_types import SPOT_EXCHANGE_IDS, SWAP_EXCHANGE_IDS
from app.services.live_read import decimal_from
from app.services.order_sizing import contract_order_amount, fetch_order_snapshot, filled_base_quantity, order_average_price, spot_market_buy
from app.services.execution_models import ExecutionResult, ExecutionStep

class CashCarryExecutor:
    reopen_cooldown_seconds = 3600
    def __init__(self, state_path: Path | None = None) -> None:
        root = Path(__file__).resolve().parents[3]
        self.state_path = state_path or root / "config" / "cash_carry_execution_state.json"
        self.state = CashCarryStateStore(self.state_path)
        self.history_quality = CashCarryHistoryQuality(self.state_path)

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
        active_counts = self.state.active_counts_by_exchange()
        ready = [
            item for item in rows
            if not item.blocked_reasons
            and ExchangeName(item.exchange) in CASH_CARRY_EXCHANGE_SET
            and not self.history_quality.blocked_reasons(ExchangeName(item.exchange), item.symbol, settings)
            and (item.exchange, item.symbol) not in blocked_keys
            and active_counts.get(ExchangeName(item.exchange), 0) < settings.cash_carry_max_positions_per_exchange
            and (allowed_open_exchanges is None or ExchangeName(item.exchange) in allowed_open_exchanges)
            and self._exposure_allows(item, settings)
        ]
        if not ready:
            probe = self._probe_open_candidate(rows, settings, blocked_keys, active_counts, allowed_open_exchanges)
            if not probe:
                return None
            item, open_settings = probe
            steps = self._open_plan(item, open_settings)
            gate_reasons = self._safety_gate(open_settings, opening=True)
            if gate_reasons:
                return self.state.remember(ExecutionResult(str(uuid.uuid4()), "blocked_by_safety_gate", " / ".join(gate_reasons), steps))
            return self._execute_open(item, open_settings, "恢复小额试单")
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
            if record.exchange not in CASH_CARRY_EXCHANGE_SET:
                continue
            live = live_by_key.get((record.exchange, record.symbol))
            if not live:
                if record.status in {"open", "mismatch"}:
                    return self._handle_missing_live_perp(record, settings)
                continue
            if live.status == "spot_only" and record.status in {"open", "mismatch"}:
                return self._handle_missing_live_perp(record, settings)
            if record.status == "mismatch" and live.status == "matched":
                self.state.mark_status(record.id, "open")
            if live.status == "mismatch" and record.status in {"open", "mismatch"}:
                rebalance = self._execute_mismatch_rebalance(record, live, settings)
                if rebalance:
                    return rebalance
            decision = cash_carry_close_decision(live.current_net_profit, live.basis_pct, live.estimated_funding_rate_pct, settings, has_live_net=True)
            if not decision.should_close:
                decision = self._turnover_close_decision(record, live, settings) or decision
            if not decision.should_close:
                decision = self._dead_position_release_decision(record, live, rows, settings) or decision
            if not decision.should_close:
                decision = self._unrecoverable_converged_loss_decision(record, live, settings) or decision
            if not decision.should_close or not self._live_close_safe(live):
                continue
            steps = self._close_plan(record, live.basis_pct, decision.reason, live.spot_quantity)
            gate_reasons = self._safety_gate(settings, opening=False)
            if gate_reasons:
                return self.state.remember(ExecutionResult(str(uuid.uuid4()), "blocked_by_safety_gate", " / ".join(gate_reasons), steps))
            return self._execute_close(record, steps, decision.reason, settings, live.spot_quantity, live.perp_base_quantity, live)
        return None

    def _execute_open(self, item: CashCarryOpportunity, settings: BotSettings, steps: list[ExecutionStep], mode_label: str = "") -> ExecutionResult:
        if ExchangeName(item.exchange) not in CASH_CARRY_EXCHANGE_SET:
            return self.state.remember(ExecutionResult(str(uuid.uuid4()), "blocked_by_safety_gate", f"{item.exchange} 已不允许正向期现开仓", steps))
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
            guard = forward_open_depth_guard(
                spot,
                swap,
                spot_symbol,
                swap_symbol,
                settings.order_notional_usdt,
                self._open_min_basis_pct(item, settings),
                min_net_profit=self.history_quality.entry_quality_gate(settings).min_net_profit,
                open_close_fee=item.estimated_open_close_fee,
                funding_income=item.estimated_funding_income,
                close_basis_pct=settings.cash_carry_close_basis_pct,
            )
            if not guard.ok:
                return self.state.remember(ExecutionResult(str(uuid.uuid4()), "blocked_by_depth", guard.reason, steps))
            self._maybe_transfer(spot, item, settings, steps[0])
            self._run(steps[1], lambda: self._set_leverage(swap, swap_symbol, settings.default_leverage, settings.margin_mode), True)
            self._verify_leverage(swap, swap_symbol, settings.default_leverage, "short", settings.margin_mode, steps[1])
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
                entry_basis_pct=item.basis_pct,
                entry_estimated_net_profit=item.estimated_net_profit,
                entry_estimated_funding_income=item.estimated_funding_income,
                entry_estimated_open_close_fee=item.estimated_open_close_fee,
                entry_notional_usdt=item.notional_usdt or settings.order_notional_usdt,
            )
            self.state.save_position(position)
            suffix = f"（{mode_label}）" if mode_label else ""
            return self.state.remember(ExecutionResult(position.id, "open_submitted", f"已提交正向期现开仓流程{suffix}", steps))
        except Exception as exc:  # noqa: BLE001
            if spot_order_id and not perp_order_id:
                rollback = self._rollback_spot_after_open_failure(spot, spot_symbol, base, base_qty)
                if rollback.get("closed"):
                    return self.state.remember(
                        ExecutionResult(
                            str(uuid.uuid4()),
                            "failed",
                            f"{self._sanitize(str(exc))}；合约未开成，已自动卖出现货回滚，避免单腿",
                            steps,
                        )
                    )
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
                    entry_basis_pct=item.basis_pct,
                    entry_estimated_net_profit=item.estimated_net_profit,
                    entry_estimated_funding_income=item.estimated_funding_income,
                    entry_estimated_open_close_fee=item.estimated_open_close_fee,
                    entry_notional_usdt=item.notional_usdt or settings.order_notional_usdt,
                )
                self.state.save_position(position)
                detail = f"{self._sanitize(str(exc))}；合约未开成，现货回滚失败 {rollback.get('reason', '未知原因')}，已记录现货孤腿"
                return self.state.remember(ExecutionResult(str(uuid.uuid4()), "failed", detail, steps))
            return self.state.remember(ExecutionResult(str(uuid.uuid4()), "failed", self._sanitize(str(exc)), steps))

    def _execute_close(
        self,
        record: CashCarryPosition,
        steps: list[ExecutionStep],
        reason: str = "",
        settings: BotSettings | None = None,
        spot_quantity: Decimal | None = None,
        perp_quantity: Decimal | None = None,
        live: CashCarryPositionRow | None = None,
    ) -> ExecutionResult:
        spot = self._exchange(record.exchange, "spot")
        swap = self._exchange(record.exchange, "swap")
        spot_symbol = f"{record.base_asset}/USDT"
        swap_symbol = f"{record.base_asset}/USDT:USDT"
        spot_qty = spot_quantity or record.quantity
        perp_qty = perp_quantity or record.quantity
        try:
            guard_floor = self._close_profit_floor(settings, reason)
            guard = forward_close_depth_guard(
                spot,
                swap,
                spot_symbol,
                swap_symbol,
                spot_qty,
                perp_qty,
                self._close_guard_spot_entry(record, live),
                self._close_guard_perp_entry(record, live),
                self._fee_rate(record.exchange),
                guard_floor,
                spot_fee_rate=self._taker_fee(record.exchange, "spot", spot_symbol),
                swap_fee_rate=self._taker_fee(record.exchange, "swap", swap_symbol),
            )
            if not guard.ok:
                return self.state.remember(ExecutionResult(record.id, "blocked_by_depth", guard.reason, steps))
            contract_qty = contract_order_amount(swap, swap_symbol, perp_qty)
            perp_order = self._run(steps[0], lambda: swap.create_order(swap_symbol, "market", "buy", contract_qty, None, {"reduceOnly": True}), True)
            spot_order = self._run(steps[1], lambda: spot.create_order(spot_symbol, "market", "sell", float(spot_qty)), True)
            close_fields = self._close_fields(spot_order, perp_order)
            close_fields["close_depth_guard"] = self._close_depth_guard_fields(guard, guard_floor)
            history = build_cash_carry_history(spot, swap, record, spot_symbol, swap_symbol, close_fields["close_spot_order_id"], close_fields["close_perp_order_id"])
            if history:
                close_fields["history"] = history
            self.state.mark_closed(record.id, reason, close_fields)
            suffix = f"：{reason}" if reason else ""
            return self.state.remember(ExecutionResult(record.id, "close_submitted", f"已提交正向期现平仓流程{suffix}", steps))
        except Exception as exc:  # noqa: BLE001
            return self.state.remember(ExecutionResult(record.id, "failed", self._sanitize(str(exc)), steps))

    def _handle_missing_live_perp(self, record: CashCarryPosition, settings: BotSettings) -> ExecutionResult:
        reason, extra = self._missing_live_perp_status(record)
        self.state.mark_status(record.id, "mismatch", reason, extra)
        history = extra.get("history") if isinstance(extra.get("history"), dict) else {}
        if not settings.cash_carry_auto_close_enabled:
            return self.state.remember(ExecutionResult(record.id, "failed", reason, []))
        if not history:
            return self.state.remember(ExecutionResult(record.id, "failed", reason, []))
        return self._execute_orphan_spot_close(record, settings, reason, history)

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

    def _execute_orphan_spot_close(
        self,
        record: CashCarryPosition,
        settings: BotSettings,
        reason: str,
        external_history: dict[str, Any],
    ) -> ExecutionResult:
        spot = self._exchange(record.exchange, "spot")
        swap = self._exchange(record.exchange, "swap")
        spot_symbol = f"{record.base_asset}/USDT"
        swap_symbol = f"{record.base_asset}/USDT:USDT"
        steps = [ExecutionStep("sell_orphan_spot", "pending", f"{reason}，自动卖出现货孤腿 {record.symbol}")]
        gate_reasons = self._safety_gate(settings, opening=False, protective=True)
        if gate_reasons:
            return self.state.remember(ExecutionResult(record.id, "blocked_by_safety_gate", " / ".join(gate_reasons), steps))
        try:
            spot_qty = min(self._spot_free_quantity(spot, record.base_asset), record.quantity)
            if spot_qty <= self._dust_quantity(record.quantity):
                return self.state.remember(ExecutionResult(record.id, "failed", f"{reason}；现货可卖数量为 0，需人工核对", steps))
            spot_order = self._run(steps[0], lambda: spot.create_order(spot_symbol, "market", "sell", float(spot_qty)), True)
            spot_order = fetch_order_snapshot(spot, spot_symbol, spot_order)
            close_spot_order_id = self._order_id(spot_order)
            close_perp_order_id = external_history.get("close_perp_order_id") or (external_history.get("short_order_ids") or [None])[-1]
            history = self._orphan_close_history(spot, swap, record, spot_symbol, swap_symbol, close_spot_order_id, close_perp_order_id)
            close_fields = {
                "close_spot_order_id": close_spot_order_id,
                "close_perp_order_id": close_perp_order_id,
                "spot_close_price": self._order_price(spot_order),
                "perp_close_price": history.get("short_close_price") or external_history.get("short_close_price"),
                "close_spot_raw": spot_order if isinstance(spot_order, dict) else None,
                "history": history or external_history,
            }
            self.state.mark_closed(record.id, f"{reason}；系统已自动卖出现货孤腿", close_fields)
            return self.state.remember(ExecutionResult(record.id, "close_submitted", f"{reason}；系统已自动卖出现货孤腿", steps))
        except Exception as exc:  # noqa: BLE001
            return self.state.remember(ExecutionResult(record.id, "failed", f"{reason}；自动卖出现货孤腿失败 {self._sanitize(str(exc))}", steps))

    def _execute_mismatch_rebalance(
        self,
        record: CashCarryPosition,
        live: CashCarryPositionRow,
        settings: BotSettings,
    ) -> ExecutionResult | None:
        if live.spot_quantity <= 0 or live.perp_base_quantity <= 0:
            return None
        gap = live.quantity_gap
        if gap == 0:
            return None
        target_qty = min(live.spot_quantity, live.perp_base_quantity)
        if target_qty <= 0:
            return None
        if gap > 0:
            return self._sell_excess_spot(record, live, gap, target_qty, settings)
        return self._reduce_excess_perp(record, live, abs(gap), target_qty, settings)

    def _sell_excess_spot(
        self,
        record: CashCarryPosition,
        live: CashCarryPositionRow,
        quantity: Decimal,
        target_qty: Decimal,
        settings: BotSettings,
    ) -> ExecutionResult:
        steps = [ExecutionStep("rebalance_sell_excess_spot", "pending", f"{record.symbol} 现货多出 {quantity}，卖出多余现货降低单腿风险")]
        gate_reasons = self._safety_gate(settings, opening=False, protective=True)
        if gate_reasons:
            return self.state.remember(ExecutionResult(record.id, "blocked_by_safety_gate", " / ".join(gate_reasons), steps))
        spot = self._exchange(record.exchange, "spot")
        spot_symbol = f"{record.base_asset}/USDT"
        try:
            order = self._run(steps[0], lambda: spot.create_order(spot_symbol, "market", "sell", float(quantity)), True)
            self.state.mark_rebalanced(
                record.id,
                target_qty,
                {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "action": "sell_excess_spot",
                    "quantity": str(quantity),
                    "target_quantity": str(target_qty),
                    "order_id": self._order_id(order),
                    "spot_quantity_before": str(live.spot_quantity),
                    "perp_base_quantity_before": str(live.perp_base_quantity),
                },
            )
            return self.state.remember(ExecutionResult(record.id, "rebalance_submitted", "已卖出多余现货，正向期现组合重新对齐", steps))
        except Exception as exc:  # noqa: BLE001
            return self.state.remember(ExecutionResult(record.id, "failed", f"卖出多余现货失败 {self._sanitize(str(exc))}", steps))

    def _reduce_excess_perp(
        self,
        record: CashCarryPosition,
        live: CashCarryPositionRow,
        quantity: Decimal,
        target_qty: Decimal,
        settings: BotSettings,
    ) -> ExecutionResult:
        steps = [ExecutionStep("rebalance_reduce_excess_perp", "pending", f"{record.symbol} 合约空单多出 {quantity}，reduceOnly 买回多余合约降低单腿风险")]
        gate_reasons = self._safety_gate(settings, opening=False, protective=True)
        if gate_reasons:
            return self.state.remember(ExecutionResult(record.id, "blocked_by_safety_gate", " / ".join(gate_reasons), steps))
        swap = self._exchange(record.exchange, "swap")
        swap_symbol = f"{record.base_asset}/USDT:USDT"
        try:
            contract_qty = contract_order_amount(swap, swap_symbol, quantity)
            order = self._run(steps[0], lambda: swap.create_order(swap_symbol, "market", "buy", contract_qty, None, {"reduceOnly": True}), True)
            self.state.mark_rebalanced(
                record.id,
                target_qty,
                {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "action": "reduce_excess_perp",
                    "quantity": str(quantity),
                    "target_quantity": str(target_qty),
                    "order_id": self._order_id(order),
                    "spot_quantity_before": str(live.spot_quantity),
                    "perp_base_quantity_before": str(live.perp_base_quantity),
                },
            )
            return self.state.remember(ExecutionResult(record.id, "rebalance_submitted", "已买回多余合约空单，正向期现组合重新对齐", steps))
        except Exception as exc:  # noqa: BLE001
            return self.state.remember(ExecutionResult(record.id, "failed", f"买回多余合约失败 {self._sanitize(str(exc))}", steps))

    def _orphan_close_history(self, spot, swap, record: CashCarryPosition, spot_symbol: str, swap_symbol: str, close_spot_order_id: str | None, close_perp_order_id: str | None) -> dict[str, Any]:
        return build_cash_carry_history(spot, swap, record, spot_symbol, swap_symbol, close_spot_order_id, close_perp_order_id)

    def _spot_free_quantity(self, exchange, base_asset: str) -> Decimal:
        balance = exchange.fetch_balance({"type": "spot"})
        item = balance.get(base_asset, {}) if isinstance(balance, dict) else {}
        return decimal_from(item.get("free") or item.get("total"))

    def _rollback_spot_after_open_failure(self, spot, spot_symbol: str, base_asset: str, quantity: Decimal) -> dict[str, Any]:
        try:
            spot_qty = min(self._spot_free_quantity(spot, base_asset), quantity)
            if spot_qty <= self._dust_quantity(quantity):
                return {"closed": False, "reason": "现货可卖数量为0"}
            order = spot.create_order(spot_symbol, "market", "sell", float(spot_qty))
            return {"closed": True, "order_id": self._order_id(order)}
        except Exception as exc:  # noqa: BLE001
            return {"closed": False, "reason": self._sanitize(str(exc))}

    def _dust_quantity(self, quantity: Decimal) -> Decimal:
        return max(Decimal("0.000001"), abs(quantity) * Decimal("0.000001"))

    def _open_plan(self, item: CashCarryOpportunity, settings: BotSettings) -> list[ExecutionStep]:
        return [
            ExecutionStep("transfer_usdt", "pending", f"按需划转 USDT，单笔名义 {settings.order_notional_usdt}"),
            ExecutionStep("set_perp_leverage", "pending", f"设置合约杠杆 {settings.default_leverage}x"),
            ExecutionStep("buy_spot", "pending", f"买入现货 {item.symbol}，数量 {item.quantity}"),
            ExecutionStep("open_perp_short", "pending", f"做空合约 {item.symbol}，数量 {item.quantity}"),
        ]

    def _probe_open_candidate(
        self,
        rows: list[CashCarryOpportunity],
        settings: BotSettings,
        blocked_keys: set[tuple[ExchangeName, str]],
        active_counts: dict[ExchangeName, int],
        allowed_open_exchanges: set[ExchangeName] | None,
    ) -> tuple[CashCarryOpportunity, BotSettings] | None:
        if not settings.cash_carry_recovery_probe_enabled or settings.cash_carry_recovery_probe_notional_usdt <= 0:
            return None
        probe_notional = min(settings.cash_carry_recovery_probe_notional_usdt, settings.order_notional_usdt)
        if probe_notional <= 0:
            return None
        probe_settings = settings.model_copy(update={"order_notional_usdt": probe_notional})
        candidates = []
        for item in rows:
            exchange = ExchangeName(item.exchange)
            if exchange not in CASH_CARRY_EXCHANGE_SET:
                continue
            if (item.exchange, item.symbol) in blocked_keys:
                continue
            if active_counts.get(exchange, 0) >= settings.cash_carry_max_positions_per_exchange:
                continue
            if allowed_open_exchanges is not None and exchange not in allowed_open_exchanges:
                continue
            if self.history_quality.blocked_reasons(exchange, item.symbol, settings):
                continue
            if not self._probe_blockers_only(item.blocked_reasons):
                continue
            adjusted = self._probe_item(item, probe_settings)
            if not self._probe_net_allows(adjusted, probe_settings):
                continue
            if not self._exposure_allows(adjusted, probe_settings):
                continue
            candidates.append(adjusted)
        if not candidates:
            return None
        return max(candidates, key=lambda row: row.estimated_net_profit), probe_settings

    def _probe_blockers_only(self, reasons: list[str]) -> bool:
        return bool(reasons) and all(reason.startswith(("V2历史胜率保护", "V3历史胜率保护")) for reason in reasons)

    def _probe_net_allows(self, item: CashCarryOpportunity, settings: BotSettings) -> bool:
        if settings.order_notional_usdt <= 0:
            return False
        min_net = settings.order_notional_usdt * settings.cash_carry_recovery_probe_min_net_pct / Decimal("100")
        return item.estimated_net_profit >= min_net

    def _probe_item(self, item: CashCarryOpportunity, settings: BotSettings) -> CashCarryOpportunity:
        if item.notional_usdt > 0:
            factor = settings.order_notional_usdt / item.notional_usdt
        else:
            factor = settings.order_notional_usdt / max(settings.order_notional_usdt, Decimal("1"))
        return item.model_copy(update={
            "quantity": q(item.quantity * factor, "0.000001"),
            "estimated_basis_profit": q(item.estimated_basis_profit * factor),
            "estimated_funding_income": q(item.estimated_funding_income * factor),
            "estimated_open_close_fee": q(item.estimated_open_close_fee * factor),
            "estimated_net_profit": q(item.estimated_net_profit * factor),
            "notional_usdt": q(settings.order_notional_usdt, "0.01"),
            "margin_required_usdt": q(settings.order_notional_usdt / settings.default_leverage if settings.default_leverage > 0 else settings.order_notional_usdt, "0.01"),
            "blocked_reasons": [],
        })

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

    def _maybe_transfer(self, exchange, item: CashCarryOpportunity, settings: BotSettings, step: ExecutionStep, amount: Decimal | None = None) -> None:
        transfer_usdt_to_spot(exchange, amount or settings.order_notional_usdt, step, settings.cash_carry_auto_transfer_enabled)

    def _run(self, step: ExecutionStep, action, enabled: bool):
        if not enabled:
            step.status = "skipped"
            step.detail += "；自动下单关闭"
            return None
        result = action()
        step.status = "done"
        step.raw = result if isinstance(result, dict) else {"result": str(result)}
        return result

    def _safety_gate(self, settings: BotSettings, opening: bool, protective: bool = False) -> list[str]:
        load_dotenv(ENV_PATH, override=False)
        reasons = []
        if not env_bool("TRADING_ENABLED"):
            reasons.append("TRADING_ENABLED 未开启")
        if not env_bool("ORDER_EXECUTION_ENABLED"):
            reasons.append("ORDER_EXECUTION_ENABLED 未开启")
        if env_bool("API_READ_ONLY_MODE", default=True):
            reasons.append("API_READ_ONLY_MODE 仍为只读")
        if settings.manual_confirm_required and not protective:
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
        except Exception as exc:  # noqa: BLE001 - repeated margin-mode setting is safe to ignore.
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
        raw = self._fetch_leverage_snapshot(exchange, symbol, margin_mode, step)
        actual = self._leverage_value(raw, side, margin_mode) or self._leverage_value(step.raw or {}, side, margin_mode)
        if actual is None:
            step.status = "failed"
            step.raw = {"expected": str(expected), "leverage": step.raw, "verification": raw}
            raise ValueError(f"{str(getattr(exchange, 'id', '')).upper()} {symbol} 未能确认实际{side}杠杆，已阻止开仓")
        if actual != expected:
            step.status = "failed"
            step.raw = {"expected": str(expected), "actual": str(actual), "leverage": step.raw, "verification": raw}
            raise ValueError(f"{str(getattr(exchange, 'id', '')).upper()} {symbol} 实际{side}杠杆 {actual}x 与参数 {expected}x 不一致，已阻止开仓")
        actual_margin = self._margin_mode_value(raw)
        if margin_mode and actual_margin and actual_margin != margin_mode:
            step.status = "failed"
            step.raw = {"expected_margin_mode": margin_mode, "actual_margin_mode": actual_margin, "leverage": step.raw, "verification": raw}
            raise ValueError(f"{str(getattr(exchange, 'id', '')).upper()} {symbol} 实际保证金模式 {actual_margin} 与参数 {margin_mode} 不一致，已阻止开仓")
        if isinstance(step.raw, dict):
            step.raw = {**step.raw, "verified_leverage": str(actual), "verification": raw}

    def _fetch_leverage_snapshot(self, exchange, symbol: str, margin_mode: str | None, step: ExecutionStep):
        if not hasattr(exchange, "fetch_leverage"):
            return {}
        try:
            if getattr(exchange, "id", "") == "okx" and margin_mode:
                return exchange.fetch_leverage(symbol, {"marginMode": margin_mode})
            return exchange.fetch_leverage(symbol)
        except Exception:
            return {}

    def _leverage_value(self, raw: dict[str, Any], side: str, margin_mode: str | None = None) -> Decimal | None:
        keys = ("shortLeverage", "isolatedShortLever") if side == "short" else ("longLeverage", "isolatedLongLever")
        cross_keys = ("crossMarginLeverage", "crossedMarginLeverage", "cross_leverage_limit")
        keys = (*keys, *cross_keys, "leverage") if margin_mode == "cross" else (*keys, "leverage", *cross_keys)
        for key in keys:
            value = self._find_key(raw, key)
            if value not in (None, ""):
                return Decimal(str(value))
        return None

    def _margin_mode_value(self, raw: dict[str, Any]) -> str | None:
        value = self._find_key(raw, "marginMode") or self._find_key(raw, "marginType") or self._find_key(raw, "mgnMode")
        if value in (None, ""):
            return None
        normalized = str(value).lower()
        if normalized in {"crossed", "cross_margin"}:
            return "cross"
        if normalized in {"regular_margin"}:
            return "cross"
        return "isolated" if normalized == "isolated" else normalized

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

    def has_active_records(self) -> bool: return bool(self.state.active_keys())

    def _exposure_allows(self, item: CashCarryOpportunity, settings: BotSettings) -> bool:
        exchange = ExchangeName(item.exchange)
        active_by_exchange = self._active_notional_by_exchange()
        active_total = sum(active_by_exchange.values(), Decimal("0"))
        order_notional = settings.order_notional_usdt
        if order_notional > settings.max_symbol_notional_usdt:
            return False
        if active_total + order_notional > settings.max_total_notional_usdt:
            return False
        if active_by_exchange.get(exchange, Decimal("0")) + order_notional > settings.single_exchange_max_notional_usdt:
            return False
        return True

    def _active_notional_by_exchange(self) -> dict[ExchangeName, Decimal]:
        result: dict[ExchangeName, Decimal] = {}
        for item in self.state.read().get("positions", []):
            if item.get("status") == "closed":
                continue
            try:
                exchange = ExchangeName(item["exchange"])
                quantity = Decimal(str(item.get("quantity") or "0"))
                spot_entry_price = Decimal(str(item.get("spot_entry_price") or "0"))
            except (ArithmeticError, KeyError, ValueError):
                continue
            notional = abs(quantity * spot_entry_price)
            result[exchange] = result.get(exchange, Decimal("0")) + notional
        return result

    def _live_close_safe(self, row: CashCarryPositionRow) -> bool:
        if row.status != "matched" or row.spot_quantity <= 0 or row.perp_base_quantity <= 0:
            return False
        tolerance = max(Decimal("0.01"), max(abs(row.spot_quantity), abs(row.perp_base_quantity)) * Decimal("0.01"))
        return abs(row.quantity_gap) <= tolerance

    def _base(self, symbol: str) -> str: return symbol.removesuffix("USDT")

    def _order_id(self, order) -> str | None: return order.get("id") if isinstance(order, dict) else None

    def _fee_rate(self, exchange: ExchangeName) -> Decimal:
        return FEE_RATES.get(ExchangeName(exchange), Decimal("0.0006"))

    def _open_min_basis_pct(self, item: CashCarryOpportunity, settings: BotSettings) -> Decimal:
        if self.history_quality.bootstrap_basis_allows(item.basis_pct, item.estimated_net_profit, settings):
            return settings.cash_carry_bootstrap_min_basis_pct
        return settings.cash_carry_min_basis_pct

    def _taker_fee(self, exchange: ExchangeName, market_type: str, symbol: str) -> Decimal:
        exchange = ExchangeName(exchange)
        cached = cached_account_taker_fee(exchange, market_type, symbol)
        if cached is not None and cached > 0:
            return cached
        exchange_obj = self._exchange(exchange, market_type)
        fee_map = account_taker_fee_map(exchange, market_type, exchange_obj)
        return fee_map.get(symbol) or self._fee_rate(exchange)

    def _close_profit_floor(self, settings: BotSettings | None, reason: str = "") -> Decimal:
        if not settings:
            return Decimal("0.5")
        base_floor = close_execution_buffer(settings)
        if "固定U止盈" in reason and settings.take_profit_usdt > 0:
            return settings.take_profit_usdt + base_floor
        if "V3死仓释放" in reason:
            return -self._dead_release_loss_cap(settings)
        if "V3恢复空间不足" in reason:
            return -settings.cash_carry_recovery_exit_max_loss_usdt
        return base_floor

    def _close_guard_spot_entry(self, record: CashCarryPosition, live: CashCarryPositionRow | None) -> Decimal:
        if live and live.spot_entry_price > 0:
            return live.spot_entry_price
        return record.spot_entry_price

    def _close_guard_perp_entry(self, record: CashCarryPosition, live: CashCarryPositionRow | None) -> Decimal:
        if live and live.perp_entry_price > 0:
            return live.perp_entry_price
        return record.perp_entry_price

    def _turnover_close_decision(
        self,
        record: CashCarryPosition,
        live: CashCarryPositionRow,
        settings: BotSettings,
    ):
        floor = close_execution_buffer(settings)
        if live.current_net_profit < floor:
            return None
        age_seconds = (datetime.now(timezone.utc) - record.opened_at).total_seconds()
        if age_seconds < 30 * 60:
            return None
        return CashCarryCloseDecision(True, f"V3周转止盈达到 {live.current_net_profit} USDT，释放交易所仓位")

    def _dead_position_release_decision(
        self,
        record: CashCarryPosition,
        live: CashCarryPositionRow,
        rows: list[CashCarryOpportunity],
        settings: BotSettings,
    ) -> CashCarryCloseDecision | None:
        if live.current_net_profit >= 0:
            return None
        age_seconds = (datetime.now(timezone.utc) - self._aware_opened_at(record)).total_seconds()
        if age_seconds < 24 * 60 * 60:
            return None
        loss = abs(live.current_net_profit)
        if loss > self._dead_release_loss_cap(settings):
            return None
        funding_income = self._recovery_funding_income(live, settings)
        max_recovery_intervals = self._turnover_recovery_interval_limit(settings)
        needed_intervals = None if funding_income <= 0 else loss / funding_income
        if funding_income > 0 and needed_intervals is not None and needed_intervals <= max_recovery_intervals:
            return None
        replacement_required_net = loss + close_execution_buffer(settings) + max(Decimal("0"), funding_income)
        replacement = self._replacement_opportunity(record, rows, replacement_required_net)
        if replacement is None:
            return None
        if funding_income > 0 and needed_intervals is not None:
            return CashCarryCloseDecision(
                True,
                f"V3低效仓位切换 {live.current_net_profit} USDT；按当前资金费约需 {needed_intervals:.1f} 期恢复，超过周转上限 {max_recovery_intervals:.1f} 期；同所替代机会 {replacement.symbol} 预估净利 {replacement.estimated_net_profit} USDT",
            )
        return CashCarryCloseDecision(
            True,
            f"V3死仓释放 {live.current_net_profit} USDT；同所替代机会 {replacement.symbol} 预估净利 {replacement.estimated_net_profit} USDT",
        )

    def _replacement_opportunity(
        self,
        record: CashCarryPosition,
        rows: list[CashCarryOpportunity],
        required_net: Decimal,
    ) -> CashCarryOpportunity | None:
        choices = []
        for item in rows:
            if ExchangeName(item.exchange) != record.exchange or item.symbol == record.symbol:
                continue
            reasons = [reason for reason in item.blocked_reasons if not self._is_open_scope_reason(reason)]
            if reasons or item.estimated_net_profit < required_net:
                continue
            choices.append(item)
        return max(choices, key=lambda item: item.estimated_net_profit) if choices else None

    def _unrecoverable_converged_loss_decision(
        self,
        record: CashCarryPosition,
        live: CashCarryPositionRow,
        settings: BotSettings,
    ) -> CashCarryCloseDecision | None:
        if live.current_net_profit >= 0:
            return None
        if live.basis_pct > settings.cash_carry_close_basis_pct:
            return None
        max_loss = settings.cash_carry_recovery_exit_max_loss_usdt
        max_intervals = settings.cash_carry_max_recovery_funding_intervals
        if max_loss <= 0 or max_intervals <= 0:
            return None
        loss = abs(live.current_net_profit)
        if loss > max_loss:
            return None
        funding_income = self._recovery_funding_income(live, settings)
        if funding_income <= 0:
            return CashCarryCloseDecision(True, f"V3恢复空间不足 {live.current_net_profit} USDT；基差已收敛且资金费无法覆盖")
        needed_intervals = loss / funding_income
        if needed_intervals <= max_intervals:
            return None
        return CashCarryCloseDecision(
            True,
            f"V3恢复空间不足 {live.current_net_profit} USDT；按当前资金费约需 {needed_intervals:.1f} 期恢复，超过 {max_intervals} 期",
        )

    def _recovery_funding_income(self, live: CashCarryPositionRow, settings: BotSettings) -> Decimal:
        if live.estimated_funding_rate_pct <= settings.cash_carry_min_funding_rate_pct:
            return Decimal("0")
        if live.estimated_funding_income > 0:
            return live.estimated_funding_income
        notional = live.perp_base_quantity * live.perp_mark_price
        if notional <= 0:
            notional = settings.order_notional_usdt
        return notional * live.estimated_funding_rate_pct / Decimal("100")

    def _turnover_recovery_interval_limit(self, settings: BotSettings) -> Decimal:
        configured = settings.cash_carry_max_recovery_funding_intervals
        if settings.cash_carry_target_daily_trades <= 0:
            return configured
        per_exchange_daily_target = Decimal(settings.cash_carry_target_daily_trades) / Decimal(len(CASH_CARRY_EXCHANGE_SET))
        if per_exchange_daily_target <= 0:
            return configured
        target_hold_hours = Decimal("24") / per_exchange_daily_target
        funding_interval_hours = Decimal("8")
        target_intervals = target_hold_hours / funding_interval_hours
        return min(configured, max(Decimal("1"), target_intervals))

    def _is_open_scope_reason(self, reason: str) -> bool:
        return "一所一币规则" in reason or "已有正向期现持仓" in reason or "持仓槽位已满" in reason

    def _dead_release_loss_cap(self, settings: BotSettings) -> Decimal:
        return min(settings.take_profit_usdt, max(Decimal("1"), settings.order_notional_usdt * Decimal("0.0075")))

    def _aware_opened_at(self, record: CashCarryPosition) -> datetime:
        return record.opened_at if record.opened_at.tzinfo else record.opened_at.replace(tzinfo=timezone.utc)

    def _close_depth_guard_fields(self, guard, min_net_profit: Decimal) -> dict[str, str]:
        return {
            "spot_price": str(guard.spot_price),
            "perp_price": str(guard.perp_price),
            "basis_pct": str(guard.basis_pct),
            "estimated_net_profit": str(guard.estimated_net_profit),
            "min_net_profit": str(min_net_profit),
        }

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
