from datetime import datetime, timezone
from decimal import Decimal
from time import sleep

from app.core.models import AIInsight, CashCarryPositionRow, ExchangeName
from app.services.ai_monitor import DeepSeekMonitor


def test_ai_monitor_does_not_reuse_cache_when_signature_changes(monkeypatch) -> None:
    monkeypatch.setattr("app.services.ai_monitor.ai_status", lambda: {"provider": "deepseek", "configured": True, "model": "deepseek-chat"})
    monitor = _InstantMonitor()
    now = datetime.now(timezone.utc)
    monitor._cache = AIInsight(provider="deepseek", model="deepseek-chat", status="ready", content="旧的后台加载结论", updated_at=now)
    monitor._cache_at = now
    monitor._cache_signature = "old-signature"

    insight = monitor.insight([], [], [], [], True, cash_carry_positions=[], strategy_switches={})

    assert insight.content == "DeepSeek 风控分析后台生成中。"
    for _ in range(20):
        if monitor._cache_signature != "old-signature":
            break
        sleep(0.01)
    assert monitor._cache_signature != "old-signature"


def test_ai_signature_ignores_small_price_tick_changes() -> None:
    monitor = DeepSeekMonitor()
    first = monitor._signature([], [], [], [], {"cash_carry_positions": [_cash_position("3.84", "-0.51")], "strategy_switches": {}})
    second = monitor._signature([], [], [], [], {"cash_carry_positions": [_cash_position("3.83", "-0.48")], "strategy_switches": {}})

    assert first == second


class _InstantMonitor(DeepSeekMonitor):
    def _refresh(self, balances, positions, opportunities, risk_events, extra, signature) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            self._cache = AIInsight(provider="deepseek", model="deepseek-chat", status="ready", content=f"sig:{signature}", updated_at=now)
            self._cache_at = now
            self._cache_signature = signature
            self._refreshing = False


def _cash_position(basis: str, net: str) -> CashCarryPositionRow:
    now = datetime.now(timezone.utc)
    return CashCarryPositionRow(
        exchange=ExchangeName.GATE,
        symbol="SPCXUSDT",
        status="matched",
        spot_quantity=Decimal("1"),
        spot_entry_price=Decimal("100"),
        spot_price=Decimal("101"),
        spot_unrealized_pnl=Decimal("1"),
        perp_side="short",
        perp_contracts=Decimal("100"),
        perp_base_quantity=Decimal("1"),
        contract_size=Decimal("0.01"),
        perp_entry_price=Decimal("104"),
        perp_mark_price=Decimal("101"),
        leverage=Decimal("5"),
        perp_unrealized_pnl=Decimal("3"),
        estimated_funding_rate_pct=Decimal("0.01"),
        estimated_funding_income=Decimal("0.01"),
        estimated_open_fee=Decimal("0.1"),
        estimated_close_fee=Decimal("0.1"),
        current_net_profit=Decimal(net),
        quantity_gap=Decimal("0"),
        basis_pct=Decimal(basis),
        updated_at=now,
    )
