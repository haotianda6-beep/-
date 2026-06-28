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
from app.services.cash_carry_execution_guard import forward_close_depth_guard, forward_open_depth_guard, forward_perp_entry_guard_after_spot
from app.services.cash_carry_execution_models import CASH_CARRY_RULESET_VERSION, CashCarryPosition
from app.services.cash_carry_history_quality import CashCarryHistoryQuality
from app.services.cash_carry_reconciler import build_cash_carry_external_perp_close_history, build_cash_carry_history
from app.services.cash_carry_quality import close_execution_buffer, estimated_entry_net_profit
from app.services.cash_carry_scope import CASH_CARRY_EXCHANGE_SET
from app.services.cash_carry_shadow_memory import CashCarryShadowMemory
from app.services.cash_carry_state import CashCarryStateStore
from app.services.cash_carry_transfer import transfer_usdt_to_spot
from app.services.exchange_factory import build_ccxt_exchange, sanitize_exchange_error
from app.services.live_market_types import SPOT_EXCHANGE_IDS, SWAP_EXCHANGE_IDS
from app.services.live_read import decimal_from
from app.services.order_sizing import contract_order_amount, fetch_order_snapshot, filled_base_quantity, order_average_price, spot_market_buy
from app.services.execution_models import ExecutionResult, ExecutionStep

class CashCarryExecutor:
    reopen_cooldown_seconds = 3600
    depth_block_cooldown_seconds = 90
    shadow_probe_min_closed = 5
    shadow_probe_min_net_pct = Decimal("0.10")
    shadow_probe_basis_buffer_pct = Decimal("0.03")
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
        depth_blocked_keys = set(self.state.recent_depth_blocked_reasons(
            self.depth_block_cooldown_seconds,
            current_basis_by_key=self._current_basis_by_key(rows),
        ))
        depth_unconfirmed_exchanges = set(self.state.recent_depth_unconfirmed_exchanges())
        active_counts = self.state.active_counts_by_exchange()
        ready = [
            item for item in rows
            if not item.blocked_reasons
            and ExchangeName(item.exchange) in CASH_CARRY_EXCHANGE_SET
            and not self.history_quality.blocked_reasons(ExchangeName(item.exchange), item.symbol, settings)
            and (item.exchange, item.symbol) not in blocked_keys
            and (ExchangeName(item.exchange), item.symbol) not in depth_blocked_keys
            and self._depth_confirmation_allows(item, depth_unconfirmed_exchanges, settings)
            and active_counts.get(ExchangeName(item.exchange), 0) < settings.cash_carry_max_positions_per_exchange
            and (allowed_open_exchanges is None or ExchangeName(item.exchange) in allowed_open_exchanges)
            and self._exposure_allows(item, settings)
        ]
        if not ready:
            probe = self._probe_open_candidate(rows, settings, blocked_keys | depth_blocked_keys, active_counts, allowed_open_exchanges)
            if not probe:
                item, reason = self._probe_open_diagnostic(rows, settings, blocked_keys | depth_blocked_keys, active_counts, allowed_open_exchanges)
                self.state.remember_probe_diagnostic(reason, item)
                return None
            self.state.clear_probe_diagnostic()
            item, open_settings, mode_label = probe
            steps = self._open_plan(item, open_settings)
            gate_reasons = self._safety_gate(open_settings, opening=True)
            if gate_reasons:
                return self.state.remember(ExecutionResult(str(uuid.uuid4()), "blocked_by_safety_gate", " / ".join(gate_reasons), steps))
            return self._execute_open(item, open_settings, steps, mode_label)
        self.state.clear_probe_diagnostic()
        item = max(ready, key=lambda row: row.estimated_net_profit)
        steps = self._open_plan(item, settings)
        gate_reasons = self._safety_gate(settings, opening=True)
        if gate_reasons:
            return self.state.remember(ExecutionResult(str(uuid.uuid4()), "blocked_by_safety_gate", " / ".join(gate_reasons), steps))
        return self._execute_open(item, settings, steps)

    def _current_basis_by_key(self, rows: list[CashCarryOpportunity]) -> dict[tuple[ExchangeName, str], Decimal]:
        return {
            (ExchangeName(item.exchange), item.symbol): item.basis_pct
            for item in rows
        }

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
                if record.status in {"open", "mismatch", "spot_only"}:
                    return self._handle_missing_live_perp(record, settings)
                continue
            if live.status == "spot_only" and record.status in {"open", "mismatch", "spot_only"}:
                return self._handle_missing_live_perp(record, settings)
            if live.status == "perp_only" and record.status in {"open", "mismatch", "perp_only"}:
                return self._execute_orphan_perp_close(record, live, settings)
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
                decision = self._legacy_stale_release_decision(record, live, settings) or decision
            if not decision.should_close:
                decision = self._unrecoverable_converged_loss_decision(record, live, rows, settings) or decision
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
                min_net_profit=self.history_quality.entry_quality_gate(settings, exchange=ExchangeName(item.exchange)).min_net_profit,
                open_close_fee=item.estimated_open_close_fee,
                funding_income=item.estimated_funding_income,
                close_basis_pct=settings.cash_carry_close_basis_pct,
            )
            if not guard.ok:
                adjusted = self._depth_adjusted_open(item, settings, spot, swap, spot_symbol, swap_symbol)
                if not adjusted:
                    return self.state.remember(ExecutionResult(str(uuid.uuid4()), "blocked_by_depth", guard.reason, steps, position=item))
                item, settings, steps = adjusted
                base_qty = item.quantity
                spot_entry_price = item.spot_price
                mode_label = self._append_mode_label(mode_label, f"深度自适应 {settings.order_notional_usdt}U")
            self._maybe_transfer(spot, item, settings, steps[0])
            self._run(steps[1], lambda: self._set_leverage(swap, swap_symbol, settings.default_leverage, settings.margin_mode), True)
            self._verify_leverage(swap, swap_symbol, settings.default_leverage, "short", settings.margin_mode, steps[1])
            spot_order_raw = self._run(steps[2], lambda: spot_market_buy(spot, spot_symbol, settings.order_notional_usdt, item.quantity), True)
            spot_order = fetch_order_snapshot(spot, spot_symbol, spot_order_raw)
            base_qty = filled_base_quantity(spot, spot_symbol, spot_order, item.quantity)
            spot_entry_price = order_average_price(spot_order, item.spot_price)
            spot_order_id = self._order_id(spot_order)
            post_spot_guard = self._post_spot_open_guard(item, settings, swap, swap_symbol, base_qty, spot_entry_price)
            if not post_spot_guard.ok:
                steps[3].status = "failed"
                steps[3].detail = post_spot_guard.reason
                rollback = self._rollback_spot_after_open_failure(spot, spot_symbol, base, base_qty)
                if rollback.get("closed"):
                    return self.state.remember(
                        ExecutionResult(
                            str(uuid.uuid4()),
                            "blocked_by_depth",
                            f"{post_spot_guard.reason}；已自动卖出现货回滚，避免低质量双腿",
                            steps,
                            position=item,
                        )
                    )
                position = CashCarryPosition(
                    id=str(uuid.uuid4()),
                    exchange=item.exchange,
                    symbol=item.symbol,
                    base_asset=base,
                    quantity=base_qty,
                    spot_entry_price=spot_entry_price,
                    perp_entry_price=post_spot_guard.perp_price if post_spot_guard.perp_price > 0 else item.perp_price,
                    spot_order_id=spot_order_id,
                    perp_order_id=None,
                    opened_at=datetime.now(timezone.utc),
                    status="spot_only",
                    entry_basis_pct=post_spot_guard.basis_pct if post_spot_guard.basis_pct else item.basis_pct,
                    entry_estimated_net_profit=post_spot_guard.estimated_net_profit,
                    entry_estimated_funding_income=item.estimated_funding_income,
                    entry_estimated_open_close_fee=item.estimated_open_close_fee,
                    entry_notional_usdt=q(base_qty * spot_entry_price, "0.01"),
                )
                self.state.save_position(position)
                return self.state.remember(
                    ExecutionResult(
                        str(uuid.uuid4()),
                        "failed",
                        f"{post_spot_guard.reason}；现货回滚失败 {rollback.get('reason', '未知原因')}，已记录现货孤腿",
                        steps,
                        position=item,
                    )
                )
            contract_qty = contract_order_amount(swap, swap_symbol, base_qty)
            perp_order_raw = self._run(
                steps[3],
                lambda: swap.create_order(swap_symbol, "market", "sell", contract_qty, None, {"reduceOnly": False, "marginMode": settings.margin_mode}),
                True,
            )
            perp_order = fetch_order_snapshot(swap, swap_symbol, perp_order_raw)
            perp_order_id = self._order_id(perp_order)
            perp_entry_price = order_average_price(perp_order, item.perp_price)
            entry_metrics = self._actual_entry_metrics(
                item,
                settings,
                base_qty,
                spot_entry_price,
                perp_entry_price,
                spot_symbol,
                swap_symbol,
            )
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
                entry_basis_pct=entry_metrics["basis_pct"],
                entry_estimated_net_profit=entry_metrics["estimated_net_profit"],
                entry_estimated_funding_income=entry_metrics["estimated_funding_income"],
                entry_estimated_open_close_fee=entry_metrics["estimated_open_close_fee"],
                entry_notional_usdt=entry_metrics["notional_usdt"],
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

    def _depth_adjusted_open(
        self,
        item: CashCarryOpportunity,
        settings: BotSettings,
        spot,
        swap,
        spot_symbol: str,
        swap_symbol: str,
    ) -> tuple[CashCarryOpportunity, BotSettings, list[ExecutionStep]] | None:
        for notional in self._depth_probe_notionals(settings):
            open_settings = settings.model_copy(update={"order_notional_usdt": notional})
            adjusted = self._probe_item(item, open_settings)
            guard = forward_open_depth_guard(
                spot,
                swap,
                spot_symbol,
                swap_symbol,
                open_settings.order_notional_usdt,
                self._open_min_basis_pct(adjusted, open_settings),
                min_net_profit=self.history_quality.entry_quality_gate(open_settings, exchange=ExchangeName(adjusted.exchange)).min_net_profit,
                open_close_fee=adjusted.estimated_open_close_fee,
                funding_income=adjusted.estimated_funding_income,
                close_basis_pct=open_settings.cash_carry_close_basis_pct,
            )
            if guard.ok:
                return adjusted, open_settings, self._open_plan(adjusted, open_settings)
        return None

    def _depth_probe_notionals(self, settings: BotSettings) -> list[Decimal]:
        if not settings.cash_carry_recovery_probe_enabled:
            return []
        order_notional = settings.order_notional_usdt
        floor = min(order_notional, settings.cash_carry_recovery_probe_notional_usdt)
        if order_notional <= 0 or floor <= 0 or floor >= order_notional:
            return []
        result: list[Decimal] = []
        for ratio in (Decimal("0.75"), Decimal("0.50"), Decimal("0.333333333333")):
            notional = q(order_notional * ratio, "0.01")
            if floor <= notional < order_notional and notional not in result:
                result.append(notional)
        floor = q(floor, "0.01")
        if floor not in result:
            result.append(floor)
        return result

    def _append_mode_label(self, current: str, extra: str) -> str:
        return f"{current}；{extra}" if current else extra

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
                realized_funding=self._realized_funding_net(swap, swap_symbol, record.opened_at),
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

    def _execute_orphan_perp_close(
        self,
        record: CashCarryPosition,
        live: CashCarryPositionRow,
        settings: BotSettings,
    ) -> ExecutionResult:
        reason = f"{record.exchange} {record.symbol} 现货腿为空，合约空单仍持有，已识别为合约孤腿"
        steps = [ExecutionStep("close_orphan_perp", "pending", f"{reason}，reduceOnly 买回合约孤腿")]
        gate_reasons = self._safety_gate(settings, opening=False, protective=True)
        if gate_reasons:
            return self.state.remember(ExecutionResult(record.id, "blocked_by_safety_gate", " / ".join(gate_reasons), steps))
        if live.perp_base_quantity <= 0:
            self.state.mark_status(record.id, "mismatch", f"{reason}；合约数量为 0，需人工核对")
            return self.state.remember(ExecutionResult(record.id, "failed", f"{reason}；合约数量为 0，需人工核对", steps))
        swap = self._exchange(record.exchange, "swap")
        swap_symbol = f"{record.base_asset}/USDT:USDT"
        try:
            contract_qty = contract_order_amount(swap, swap_symbol, live.perp_base_quantity)
            order = self._run(steps[0], lambda: swap.create_order(swap_symbol, "market", "buy", contract_qty, None, {"reduceOnly": True}), True)
            close_fields = {
                "close_perp_order_id": self._order_id(order),
                "perp_close_price": self._order_price(order),
                "close_perp_raw": order if isinstance(order, dict) else None,
            }
            self.state.mark_closed(record.id, f"{reason}；系统已自动买回合约孤腿", close_fields)
            return self.state.remember(ExecutionResult(record.id, "close_submitted", f"{reason}；系统已自动买回合约孤腿", steps))
        except Exception as exc:  # noqa: BLE001
            self.state.mark_status(record.id, "perp_only", f"{reason}；自动买回合约孤腿失败 {self._sanitize(str(exc))}")
            return self.state.remember(ExecutionResult(record.id, "failed", f"{reason}；自动买回合约孤腿失败 {self._sanitize(str(exc))}", steps))

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
    ) -> tuple[CashCarryOpportunity, BotSettings, str] | None:
        if not settings.cash_carry_recovery_probe_enabled or settings.cash_carry_recovery_probe_notional_usdt <= 0:
            return None
        probe_notional = min(settings.cash_carry_recovery_probe_notional_usdt, settings.order_notional_usdt)
        if probe_notional <= 0:
            return None
        base_probe_settings = settings.model_copy(update={"order_notional_usdt": probe_notional})
        candidates: list[tuple[CashCarryOpportunity, BotSettings, str]] = []
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
            if not self._depth_confirmation_allows(item, self.state.recent_depth_unconfirmed_exchanges(), base_probe_settings):
                continue
            candidate_settings = base_probe_settings
            adjusted = self._probe_item(item, candidate_settings)
            mode_label = ""
            if self._probe_blockers_only(item.blocked_reasons):
                if not self._probe_net_allows(adjusted, candidate_settings):
                    continue
                mode_label = "恢复小额试单"
            elif self._shadow_probe_blockers_only(item.blocked_reasons):
                candidate_settings = self._shadow_probe_settings(item, base_probe_settings)
                adjusted = self._probe_item(item, candidate_settings)
                if not self._shadow_probe_allows(item, adjusted, settings, candidate_settings):
                    continue
                mode_label = "影子样本小额探索"
            else:
                continue
            if not self._exposure_allows(adjusted, candidate_settings):
                continue
            candidates.append((adjusted, candidate_settings, mode_label))
        if not candidates:
            return None
        return max(candidates, key=lambda row: row[0].estimated_net_profit)

    def _probe_open_diagnostic(
        self,
        rows: list[CashCarryOpportunity],
        settings: BotSettings,
        blocked_keys: set[tuple[ExchangeName, str]],
        active_counts: dict[ExchangeName, int],
        allowed_open_exchanges: set[ExchangeName] | None,
    ) -> tuple[CashCarryOpportunity | None, str]:
        if not rows:
            return None, "没有正向期现候选，等待扫描数据"
        if not settings.cash_carry_recovery_probe_enabled or settings.cash_carry_recovery_probe_notional_usdt <= 0:
            return None, "小额探索开关关闭或小额本金为0"
        probe_notional = min(settings.cash_carry_recovery_probe_notional_usdt, settings.order_notional_usdt)
        if probe_notional <= 0:
            return None, "小额探索本金无效"
        base_probe_settings = settings.model_copy(update={"order_notional_usdt": probe_notional})
        depth_unconfirmed = self.state.recent_depth_unconfirmed_exchanges()
        diagnostics: list[tuple[Decimal, Decimal, CashCarryOpportunity, str]] = []
        for item in rows:
            exchange = ExchangeName(item.exchange)
            reason = self._probe_filter_reason(
                item,
                settings,
                base_probe_settings,
                blocked_keys,
                active_counts,
                allowed_open_exchanges,
                depth_unconfirmed,
            )
            if reason is None:
                candidate_settings = base_probe_settings
                adjusted = self._probe_item(item, candidate_settings)
                if self._probe_blockers_only(item.blocked_reasons):
                    reason = self._probe_net_reject_reason(adjusted, candidate_settings) or "恢复小额试单条件已满足，等待执行锁"
                elif self._shadow_probe_blockers_only(item.blocked_reasons):
                    candidate_settings = self._shadow_probe_settings(item, base_probe_settings)
                    adjusted = self._probe_item(item, candidate_settings)
                    reason = self._shadow_probe_reject_reason(item, adjusted, settings, candidate_settings) or "影子样本小额探索条件已满足，等待执行锁"
                else:
                    reason = "包含硬风险原因：" + " / ".join(item.blocked_reasons[:3])
                if reason.startswith(("恢复小额试单条件已满足", "影子样本小额探索条件已满足")) and not self._exposure_allows(adjusted, candidate_settings):
                    reason = "仓位额度不足或超过单交易所/单币种敞口限制"
            diagnostics.append((item.estimated_net_profit, item.basis_pct, item, reason))
        if not diagnostics:
            return None, "没有 Gate/Bitget 范围内候选"
        _net, _basis, item, reason = max(diagnostics, key=lambda row: (row[0], row[1]))
        return item, f"{ExchangeName(item.exchange).value} {item.symbol} 未进入小额探索：{reason}"

    def _probe_filter_reason(
        self,
        item: CashCarryOpportunity,
        settings: BotSettings,
        probe_settings: BotSettings,
        blocked_keys: set[tuple[ExchangeName, str]],
        active_counts: dict[ExchangeName, int],
        allowed_open_exchanges: set[ExchangeName] | None,
        depth_unconfirmed_exchanges: dict[ExchangeName, str],
    ) -> str | None:
        exchange = ExchangeName(item.exchange)
        if exchange not in CASH_CARRY_EXCHANGE_SET:
            return f"{exchange.value} 不在正向期现允许范围"
        if (exchange, item.symbol) in blocked_keys:
            return "该币已有持仓、刚关闭或最近执行深度失败仍在冷却"
        if active_counts.get(exchange, 0) >= settings.cash_carry_max_positions_per_exchange:
            return f"{exchange.value} 持仓槽位已满 {active_counts.get(exchange, 0)}/{settings.cash_carry_max_positions_per_exchange}"
        if allowed_open_exchanges is not None and exchange not in allowed_open_exchanges:
            return f"{exchange.value} 暂不在当前允许开仓交易所集合"
        history_reasons = self.history_quality.blocked_reasons(exchange, item.symbol, settings)
        if history_reasons:
            return "历史风控拦截：" + " / ".join(history_reasons[:2])
        if not self._depth_confirmation_allows(item, depth_unconfirmed_exchanges, probe_settings):
            return depth_unconfirmed_exchanges.get(exchange, "交易所近期深度失败，等待深度重新确认")
        return None

    def _probe_blockers_only(self, reasons: list[str]) -> bool:
        return bool(reasons) and all(reason.startswith(("V2历史胜率保护", "V3历史胜率保护")) for reason in reasons)

    def _shadow_probe_blockers_only(self, reasons: list[str]) -> bool:
        allowed = (
            "合约溢价未达",
            "回归到平仓线后的净利预估",
            "V3冷启动净利预估",
            "V3频率调节净利预估",
            "V2历史胜率保护",
            "V3历史胜率保护",
            "信号持续不足",
            "基差波动过大",
            "基差分位样本不足",
            "基差分位不足",
        )
        return bool(reasons) and all(reason.startswith(allowed) or self._is_depth_exchange_confirmation_reason(reason) for reason in reasons)

    def _is_depth_exchange_confirmation_reason(self, reason: str) -> bool:
        return "执行前盘口深度失败" in reason and "等待盘口深度确认" in reason

    def _probe_net_allows(self, item: CashCarryOpportunity, settings: BotSettings) -> bool:
        if settings.order_notional_usdt <= 0:
            return False
        min_net = settings.order_notional_usdt * settings.cash_carry_recovery_probe_min_net_pct / Decimal("100")
        return item.estimated_net_profit >= min_net

    def _probe_net_reject_reason(self, item: CashCarryOpportunity, settings: BotSettings) -> str | None:
        if settings.order_notional_usdt <= 0:
            return "小额本金无效"
        min_net = settings.order_notional_usdt * settings.cash_carry_recovery_probe_min_net_pct / Decimal("100")
        if item.estimated_net_profit < min_net:
            return f"恢复小额试单净利 {item.estimated_net_profit:.4f}U < 门槛 {min_net:.4f}U"
        return None

    def _shadow_probe_settings(self, item: CashCarryOpportunity, settings: BotSettings) -> BotSettings:
        basis_floor = max(settings.cash_carry_close_basis_pct, item.basis_pct - self.shadow_probe_basis_buffer_pct)
        return settings.model_copy(update={
            "cash_carry_min_basis_pct": min(settings.cash_carry_min_basis_pct, basis_floor),
            "cash_carry_bootstrap_min_basis_pct": min(settings.cash_carry_bootstrap_min_basis_pct, basis_floor),
        })

    def _shadow_probe_allows(
        self,
        original: CashCarryOpportunity,
        adjusted: CashCarryOpportunity,
        base_settings: BotSettings,
        probe_settings: BotSettings,
    ) -> bool:
        return self._shadow_probe_reject_reason(original, adjusted, base_settings, probe_settings) is None

    def _shadow_probe_reject_reason(
        self,
        original: CashCarryOpportunity,
        adjusted: CashCarryOpportunity,
        base_settings: BotSettings,
        probe_settings: BotSettings,
    ) -> str | None:
        summary = CashCarryShadowMemory(self.state_path).summary(exchange=ExchangeName(original.exchange))
        if summary.closed_count < self.shadow_probe_min_closed:
            return f"影子样本不足 {summary.closed_count}/{self.shadow_probe_min_closed}"
        target_win = base_settings.cash_carry_target_win_rate_pct or Decimal("70")
        if summary.win_rate_pct < target_win or summary.total_estimated_net <= 0:
            return f"影子胜率或累计净利不足：胜率 {summary.win_rate_pct:.2f}% / 累计 {summary.total_estimated_net:.4f}U"
        if summary.worst_estimated_net < -close_execution_buffer(base_settings):
            return f"影子最差亏损 {summary.worst_estimated_net:.4f}U 超过执行缓冲 {-close_execution_buffer(base_settings):.4f}U"
        if summary.min_winning_entry_basis_pct is not None:
            required_basis = max(Decimal("0"), summary.min_winning_entry_basis_pct - self.shadow_probe_basis_buffer_pct)
            if original.basis_pct < required_basis:
                return f"当前基差 {original.basis_pct:.4f}% < 影子赢家最低入场门槛 {required_basis:.4f}%"
        if probe_settings.order_notional_usdt <= 0 or base_settings.order_notional_usdt <= 0:
            return "小额本金或基础本金无效"
        scale = probe_settings.order_notional_usdt / base_settings.order_notional_usdt
        expected_shadow_net = summary.avg_estimated_net * scale
        min_shadow_net = max(Decimal("0.05"), probe_settings.order_notional_usdt * self.shadow_probe_min_net_pct / Decimal("100"))
        if expected_shadow_net < min_shadow_net:
            return f"影子均值按小额折算 {expected_shadow_net:.4f}U < 门槛 {min_shadow_net:.4f}U"
        min_allowed_net = -close_execution_buffer(probe_settings)
        if adjusted.estimated_net_profit < min_allowed_net:
            return f"当前小额净利 {adjusted.estimated_net_profit:.4f}U < 允许下限 {min_allowed_net:.4f}U"
        return None

    def _depth_confirmation_allows(
        self,
        item: CashCarryOpportunity,
        depth_unconfirmed_exchanges: set[ExchangeName] | dict[ExchangeName, str],
        settings: BotSettings,
    ) -> bool:
        exchange = ExchangeName(item.exchange)
        if exchange not in depth_unconfirmed_exchanges:
            return True
        required = item.notional_usdt if item.notional_usdt > 0 else settings.order_notional_usdt
        if item.max_safe_notional_usdt is None:
            return True
        return item.max_safe_notional_usdt is not None and item.max_safe_notional_usdt >= required

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

    def _actual_entry_metrics(
        self,
        item: CashCarryOpportunity,
        settings: BotSettings,
        base_qty: Decimal,
        spot_entry_price: Decimal,
        perp_entry_price: Decimal,
        spot_symbol: str,
        swap_symbol: str,
    ) -> dict[str, Decimal]:
        notional = base_qty * spot_entry_price
        if base_qty <= 0 or spot_entry_price <= 0 or perp_entry_price <= 0 or notional <= 0:
            return {
                "basis_pct": item.basis_pct,
                "estimated_net_profit": item.estimated_net_profit,
                "estimated_funding_income": item.estimated_funding_income,
                "estimated_open_close_fee": item.estimated_open_close_fee,
                "notional_usdt": item.notional_usdt or settings.order_notional_usdt,
            }
        basis_pct = (perp_entry_price - spot_entry_price) / spot_entry_price * Decimal("100")
        funding_rate = item.funding_rate_pct / Decimal("100")
        funding_income = notional * funding_rate
        exchange_name = ExchangeName(item.exchange)
        spot_fee = self._safe_taker_fee(exchange_name, "spot", spot_symbol)
        swap_fee = self._safe_taker_fee(exchange_name, "swap", swap_symbol)
        open_close_fee = (
            base_qty * spot_entry_price * spot_fee
            + base_qty * perp_entry_price * swap_fee
        ) * Decimal("2")
        actual_settings = settings.model_copy(update={"order_notional_usdt": notional})
        estimated_net = estimated_entry_net_profit(actual_settings, basis_pct, funding_rate, open_close_fee)
        return {
            "basis_pct": q(basis_pct),
            "estimated_net_profit": q(estimated_net),
            "estimated_funding_income": q(funding_income),
            "estimated_open_close_fee": q(open_close_fee),
            "notional_usdt": q(notional, "0.01"),
        }

    def _realized_funding_net(self, swap, swap_symbol: str, opened_at: datetime) -> Decimal:
        if not getattr(swap, "has", {}).get("fetchFundingHistory"):
            return Decimal("0")
        try:
            since = int(self._aware_datetime(opened_at).timestamp() * 1000)
            now = datetime.now(timezone.utc)
            total = Decimal("0")
            for item in swap.fetch_funding_history(swap_symbol, since=since, limit=100):
                timestamp = item.get("timestamp")
                if timestamp:
                    at = datetime.fromtimestamp(int(timestamp) / 1000, tz=timezone.utc)
                    if at < self._aware_datetime(opened_at) or at > now:
                        continue
                total += decimal_from(item.get("amount"))
            return total
        except Exception:
            return Decimal("0")

    def _post_spot_open_guard(
        self,
        item: CashCarryOpportunity,
        settings: BotSettings,
        swap,
        swap_symbol: str,
        base_qty: Decimal,
        spot_entry_price: Decimal,
    ):
        notional = base_qty * spot_entry_price
        reference = item.notional_usdt or settings.order_notional_usdt
        factor = notional / reference if reference > 0 and notional > 0 else Decimal("1")
        gate = self.history_quality.entry_quality_gate(settings, exchange=ExchangeName(item.exchange))
        return forward_perp_entry_guard_after_spot(
            swap,
            swap_symbol,
            base_qty,
            spot_entry_price,
            self._open_min_basis_pct(item, settings),
            min_net_profit=gate.min_net_profit * factor,
            open_close_fee=item.estimated_open_close_fee * factor,
            funding_income=item.estimated_funding_income * factor,
            close_basis_pct=settings.cash_carry_close_basis_pct,
        )

    def _safe_taker_fee(self, exchange: ExchangeName, market_type: str, symbol: str) -> Decimal:
        try:
            fee = self._taker_fee(exchange, market_type, symbol)
        except Exception:  # noqa: BLE001 - metrics recording must not leave an opened pair untracked.
            return self._fee_rate(exchange)
        return fee if fee > 0 else self._fee_rate(exchange)

    def _open_min_basis_pct(self, item: CashCarryOpportunity, settings: BotSettings) -> Decimal:
        if self.history_quality.bootstrap_basis_allows(item.basis_pct, item.estimated_net_profit, settings, exchange=ExchangeName(item.exchange)):
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
        if "V3恢复空间不足" in reason or "V3亏损切换" in reason:
            return -settings.cash_carry_recovery_exit_max_loss_usdt
        if "旧规则低效仓位释放" in reason:
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

    def _legacy_stale_release_decision(
        self,
        record: CashCarryPosition,
        live: CashCarryPositionRow,
        settings: BotSettings,
    ) -> CashCarryCloseDecision | None:
        if record.strategy_version == CASH_CARRY_RULESET_VERSION:
            return None
        if live.current_net_profit >= 0:
            return None
        if live.basis_pct < settings.cash_carry_min_basis_pct:
            return None
        age_seconds = (datetime.now(timezone.utc) - self._aware_opened_at(record)).total_seconds()
        if age_seconds < 24 * 60 * 60:
            return None
        loss = abs(live.current_net_profit)
        release_cap = min(
            settings.cash_carry_recovery_exit_max_loss_usdt,
            max(settings.take_profit_usdt, self._dead_release_loss_cap(settings)),
        )
        if release_cap <= 0 or loss > release_cap:
            return None
        funding_income = self._recovery_funding_income(live, settings)
        max_intervals = self._turnover_recovery_interval_limit(settings)
        needed_intervals = None if funding_income <= 0 else loss / funding_income
        if funding_income > 0 and needed_intervals is not None and needed_intervals <= max_intervals:
            return None
        age_hours = Decimal(str(age_seconds / 3600))
        if funding_income > 0 and needed_intervals is not None:
            return CashCarryCloseDecision(
                True,
                f"旧规则低效仓位释放 {live.current_net_profit} USDT；已持仓 {age_hours:.1f} 小时，按当前资金费约需 {needed_intervals:.1f} 期恢复，超过周转上限 {max_intervals:.1f} 期，释放交易所槽位",
            )
        return CashCarryCloseDecision(
            True,
            f"旧规则低效仓位释放 {live.current_net_profit} USDT；已持仓 {age_hours:.1f} 小时，当前资金费无法覆盖恢复，释放交易所槽位",
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
        rows: list[CashCarryOpportunity],
        settings: BotSettings,
    ) -> CashCarryCloseDecision | None:
        if live.current_net_profit >= 0:
            return None
        if live.basis_pct > settings.cash_carry_close_basis_pct:
            return None
        max_loss = settings.cash_carry_recovery_exit_max_loss_usdt
        max_intervals = self._turnover_recovery_interval_limit(settings)
        if max_loss <= 0 or max_intervals <= 0:
            return None
        loss = abs(live.current_net_profit)
        if loss > max_loss:
            return None
        funding_income = self._recovery_funding_income(live, settings)
        needed_intervals = None if funding_income <= 0 else loss / funding_income
        if needed_intervals is not None and needed_intervals <= max_intervals:
            return None
        replacement_required_net = loss + close_execution_buffer(settings) + max(Decimal("0"), funding_income)
        replacement = self._replacement_opportunity(record, rows, replacement_required_net)
        if replacement is None:
            return self._legacy_slot_release_decision(record, live, loss, funding_income, needed_intervals, max_intervals, settings)
        if funding_income <= 0:
            return CashCarryCloseDecision(
                True,
                f"V3亏损切换 {live.current_net_profit} USDT；基差已收敛且资金费无法覆盖；同所替代机会 {replacement.symbol} 预估净利 {replacement.estimated_net_profit} USDT",
            )
        return CashCarryCloseDecision(
            True,
            f"V3亏损切换 {live.current_net_profit} USDT；按当前资金费约需 {needed_intervals:.1f} 期恢复，超过 {max_intervals} 期；同所替代机会 {replacement.symbol} 预估净利 {replacement.estimated_net_profit} USDT",
        )

    def _legacy_slot_release_decision(
        self,
        record: CashCarryPosition,
        live: CashCarryPositionRow,
        loss: Decimal,
        funding_income: Decimal,
        needed_intervals: Decimal | None,
        max_intervals: Decimal,
        settings: BotSettings,
    ) -> CashCarryCloseDecision | None:
        if record.strategy_version == CASH_CARRY_RULESET_VERSION:
            return None
        age_seconds = (datetime.now(timezone.utc) - self._aware_opened_at(record)).total_seconds()
        if age_seconds < 24 * 60 * 60:
            return None
        cap = self._dead_release_loss_cap(settings)
        if loss > cap:
            return None
        if funding_income > 0 and needed_intervals is not None:
            return CashCarryCloseDecision(
                True,
                f"旧规则低效仓位释放 {live.current_net_profit} USDT；基差已收敛，按当前资金费约需 {needed_intervals:.1f} 期恢复，超过周转上限 {max_intervals} 期，释放交易所槽位",
            )
        return CashCarryCloseDecision(
            True,
            f"旧规则低效仓位释放 {live.current_net_profit} USDT；基差已收敛且资金费无法覆盖，释放交易所槽位",
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

    def _aware_datetime(self, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

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
