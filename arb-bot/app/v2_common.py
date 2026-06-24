from __future__ import annotations

from decimal import Decimal

from app.models import OpenPair, StrategyState, utc_now_ms
from app.v2_support import target_exit_spread


class V2CommonMixin:
    def _display_exit_spread(self, pair: OpenPair) -> Decimal:
        return self.exit_target_spread if self.exit_target_spread is not None else target_exit_spread(self.settings, pair)

    def _clear_terminal_order(self) -> None:
        if self.active_order and self.active_order.executed_qty > 0:
            self._recover("币安限价单已结束但存在成交数量，进入自动恢复流程")
            return
        self.active_order = None
        self.runtime.state = StrategyState.PAIR_OPEN if self.runtime.open_pair else StrategyState.IDLE

    def _check_mt4_timeout(self, started_ms: int, message: str) -> None:
        if started_ms and utc_now_ms() - started_ms > self.settings.max_hedge_delay_ms:
            handler = getattr(self, "_handle_mt4_timeout", None)
            if handler:
                handler(message)
                return
            self._recover(message)

    def _pause(self, reason: str) -> None:
        self._recover(reason)

    def _recover(self, reason: str) -> None:
        self.runtime.last_error = reason
        if self.active_order:
            self.runtime.state = StrategyState.QUOTING_BINANCE_EXIT if self.active_order.reduce_only else StrategyState.QUOTING_BINANCE_ENTRY
        elif getattr(self, "pending_close_tickets", None):
            self.runtime.state = StrategyState.CLOSING_MT4
        elif getattr(self, "hedge_command_id", None):
            self.runtime.state = StrategyState.HEDGING_MT4
        elif self.runtime.open_pair:
            self.runtime.state = StrategyState.PAIR_OPEN
        else:
            self.runtime.state = StrategyState.IDLE
        self.storage.record_event("v2_recovering", {"reason": reason, "state": self.runtime.state.value})

    def _clear_to_idle(self) -> None:
        self.clear()
        self.runtime.state = StrategyState.PAIR_OPEN if self.runtime.open_pair else StrategyState.IDLE
