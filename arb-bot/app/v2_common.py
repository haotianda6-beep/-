from __future__ import annotations

from decimal import Decimal

from app.models import OpenPair, StrategyState, utc_now_ms
from app.v2_support import target_exit_spread


class V2CommonMixin:
    def _display_exit_spread(self, pair: OpenPair) -> Decimal:
        return self.exit_target_spread if self.exit_target_spread is not None else target_exit_spread(self.settings, pair)

    def _clear_terminal_order(self) -> None:
        if self.active_order and self.active_order.executed_qty > 0:
            self._pause("币安限价单已结束但存在成交数量，请人工确认")
            return
        self.active_order = None
        self.runtime.state = StrategyState.PAIR_OPEN if self.runtime.open_pair else StrategyState.IDLE

    def _check_mt4_timeout(self, started_ms: int, message: str) -> None:
        if started_ms and utc_now_ms() - started_ms > self.settings.max_hedge_delay_ms:
            self._pause(message)

    def _pause(self, reason: str) -> None:
        self.runtime.last_error = reason
        self.runtime.state = StrategyState.PAUSED
        self.storage.record_event("v2_paused", {"reason": reason})

    def _clear_to_idle(self) -> None:
        self.clear()
        self.runtime.state = StrategyState.PAIR_OPEN if self.runtime.open_pair else StrategyState.IDLE
