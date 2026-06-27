from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.core.models import BotSettings, CashCarryOpportunity, CashCarryPositionRow, DataSource, ExchangeName
from app.services.cash_carry_execution_models import CASH_CARRY_RULESET_VERSION
from app.services.cash_carry_executor import CashCarryExecutor
from app.services.order_sizing import contract_order_amount
from app.services.execution_models import ExecutionResult, ExecutionStep


def test_cash_carry_executor_blocks_until_trade_subswitch_is_enabled(tmp_path) -> None:
    executor = CashCarryExecutor(tmp_path / "state.json")
    settings = BotSettings(
        manual_confirm_required=False,
        cash_carry_auto_open_enabled=True,
        cash_carry_auto_trade_enabled=False,
    )

    result = executor.evaluate_open([_opportunity()], settings)

    assert result is not None
    assert result.status == "blocked_by_safety_gate"
    assert "正向期现自动下单未开启" in result.reason
    assert [step.name for step in result.steps] == [
        "transfer_usdt",
        "set_perp_leverage",
        "buy_spot",
        "open_perp_short",
    ]


def test_cash_carry_executor_does_not_open_when_strategy_switch_is_off(tmp_path) -> None:
    executor = CashCarryExecutor(tmp_path / "state.json")

    assert executor.evaluate_open([_opportunity()], BotSettings(cash_carry_auto_open_enabled=False)) is None


def test_cash_carry_executor_respects_global_open_lock(tmp_path) -> None:
    executor = CashCarryExecutor(tmp_path / "state.json")
    settings = BotSettings(manual_confirm_required=False, cash_carry_auto_open_enabled=True, cash_carry_auto_trade_enabled=False)

    assert executor.evaluate_open([_opportunity()], settings, allow_open=False) is None


def test_cash_carry_executor_skips_existing_exchange_and_checks_next_exchange(tmp_path) -> None:
    state = tmp_path / "state.json"
    state.write_text('{"positions":[{"exchange":"GATE","symbol":"ABCUSDT","status":"open"}]}', encoding="utf-8")
    executor = CashCarryExecutor(state)
    settings = BotSettings(manual_confirm_required=False, cash_carry_auto_open_enabled=True, cash_carry_auto_trade_enabled=False)

    result = executor.evaluate_open([_opportunity("ABCUSDT", "9"), _opportunity("XYZUSDT", "1", exchange=ExchangeName.BITGET)], settings)

    assert result is not None
    assert result.status == "blocked_by_safety_gate"
    assert "XYZUSDT" in result.steps[2].detail


def test_cash_carry_executor_does_not_duplicate_existing_ready_position(tmp_path) -> None:
    state = tmp_path / "state.json"
    state.write_text('{"positions":[{"exchange":"GATE","symbol":"ABCUSDT","status":"mismatch"}]}', encoding="utf-8")
    executor = CashCarryExecutor(state)
    settings = BotSettings(manual_confirm_required=False, cash_carry_auto_open_enabled=True, cash_carry_auto_trade_enabled=False)

    assert executor.evaluate_open([_opportunity("ABCUSDT", "9")], settings) is None


def test_cash_carry_executor_ignores_removed_exchanges(tmp_path) -> None:
    executor = CashCarryExecutor(tmp_path / "state.json")
    settings = BotSettings(manual_confirm_required=False, cash_carry_auto_open_enabled=True, cash_carry_auto_trade_enabled=False)

    assert executor.evaluate_open([_opportunity("XYZUSDT", "9", exchange=ExchangeName.BYBIT)], settings) is None


def test_cash_carry_executor_blocks_same_exchange_new_symbol_when_slots_are_full(tmp_path) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        '{"positions":[{"exchange":"BITGET","symbol":"JCTUSDT","base_asset":"JCT","quantity":"85483.5902","spot_entry_price":"0.0058373725531173","perp_entry_price":"0.00586","opened_at":"2026-06-12T00:00:00+00:00","status":"open"}]}',
        encoding="utf-8",
    )
    executor = CashCarryExecutor(state)
    settings = BotSettings(
        order_notional_usdt=Decimal("500"),
        single_exchange_max_notional_usdt=Decimal("1000"),
        max_total_notional_usdt=Decimal("2000"),
        max_symbol_notional_usdt=Decimal("500"),
        cash_carry_max_positions_per_exchange=1,
        manual_confirm_required=False,
        cash_carry_auto_open_enabled=True,
        cash_carry_auto_trade_enabled=False,
    )

    assert executor.evaluate_open([_opportunity("SKYAIUSDT", "3", exchange=ExchangeName.BITGET)], settings) is None


def test_cash_carry_executor_allows_same_exchange_new_symbol_when_slot_is_available(tmp_path) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        '{"positions":[{"exchange":"BITGET","symbol":"JCTUSDT","base_asset":"JCT","quantity":"90000","spot_entry_price":"0.006","perp_entry_price":"0.0061","opened_at":"2026-06-12T00:00:00+00:00","status":"open"}]}',
        encoding="utf-8",
    )
    executor = CashCarryExecutor(state)
    settings = BotSettings(
        order_notional_usdt=Decimal("300"),
        single_exchange_max_notional_usdt=Decimal("1000"),
        max_total_notional_usdt=Decimal("2000"),
        max_symbol_notional_usdt=Decimal("500"),
        cash_carry_max_positions_per_exchange=2,
        manual_confirm_required=False,
        cash_carry_auto_open_enabled=True,
        cash_carry_auto_trade_enabled=False,
    )

    result = executor.evaluate_open([_opportunity("SKYAIUSDT", "3", exchange=ExchangeName.BITGET)], settings)

    assert result is not None
    assert result.status == "blocked_by_safety_gate"
    assert "SKYAIUSDT" in result.steps[2].detail


def test_cash_carry_executor_blocks_historical_loser_even_if_candidate_is_ready(tmp_path) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        '{"positions":[{"exchange":"GATE","symbol":"ABCUSDT","status":"closed","history":{"actual_net_profit":"-2.1"}}]}',
        encoding="utf-8",
    )
    executor = CashCarryExecutor(state)
    settings = BotSettings(
        order_notional_usdt=Decimal("300"),
        manual_confirm_required=False,
        cash_carry_auto_open_enabled=True,
        cash_carry_auto_trade_enabled=False,
    )

    assert executor.evaluate_open([_opportunity("ABCUSDT", "3", exchange=ExchangeName.GATE)], settings) is None


def test_cash_carry_executor_allows_small_recovery_probe_only_for_global_win_rate_gate(tmp_path) -> None:
    executor = CashCarryExecutor(tmp_path / "state.json")
    settings = BotSettings(
        order_notional_usdt=Decimal("300"),
        cash_carry_recovery_probe_notional_usdt=Decimal("100"),
        manual_confirm_required=False,
        cash_carry_auto_open_enabled=True,
        cash_carry_auto_trade_enabled=False,
    )
    candidate = _opportunity("ABCUSDT", "3", exchange=ExchangeName.GATE).model_copy(update={
        "notional_usdt": Decimal("300"),
        "blocked_reasons": ["V3历史胜率保护：净利预估 3.0000U < 动态安全垫 6.0000U"],
    })

    result = executor.evaluate_open([candidate], settings)

    assert result is not None
    assert result.status == "blocked_by_safety_gate"
    assert "正向期现自动下单未开启" in result.reason
    assert "单笔名义 100" in result.steps[0].detail
    assert "数量 0.333333" in result.steps[2].detail


def test_cash_carry_executor_recovery_probe_does_not_bypass_other_blockers(tmp_path) -> None:
    executor = CashCarryExecutor(tmp_path / "state.json")
    settings = BotSettings(
        order_notional_usdt=Decimal("300"),
        cash_carry_recovery_probe_notional_usdt=Decimal("100"),
        manual_confirm_required=False,
        cash_carry_auto_open_enabled=True,
        cash_carry_auto_trade_enabled=False,
    )
    candidate = _opportunity("ABCUSDT", "3", exchange=ExchangeName.GATE).model_copy(update={
        "notional_usdt": Decimal("300"),
        "blocked_reasons": [
            "V3历史胜率保护：净利预估 3.0000U < 动态安全垫 6.0000U",
            "现货/合约最低24h成交量低于 300000U",
        ],
    })

    assert executor.evaluate_open([candidate], settings) is None


def test_cash_carry_executor_skips_recent_depth_failed_symbol(tmp_path) -> None:
    state = tmp_path / "state.json"
    blocked_at = datetime.now(timezone.utc).isoformat()
    state.write_text(
        f'{{"positions":[],"last_result":{{"status":"blocked_by_depth","exchange":"GATE","symbol":"ABCUSDT","reason":"深度均价开仓基差 -0.2469% 低于 0.6%","at":"{blocked_at}"}}}}',
        encoding="utf-8",
    )
    executor = CashCarryExecutor(state)
    settings = BotSettings(manual_confirm_required=False, cash_carry_auto_open_enabled=True, cash_carry_auto_trade_enabled=False)

    assert executor.state.recent_depth_blocked_reasons(executor.depth_block_cooldown_seconds)
    assert executor.evaluate_open([_opportunity("ABCUSDT", "3", exchange=ExchangeName.GATE)], settings) is None


def test_cash_carry_executor_depth_failure_does_not_block_other_symbol(tmp_path) -> None:
    state = tmp_path / "state.json"
    blocked_at = datetime.now(timezone.utc).isoformat()
    state.write_text(
        f'{{"positions":[],"last_result":{{"status":"blocked_by_depth","exchange":"GATE","symbol":"ABCUSDT","reason":"深度均价开仓基差 -0.2469% 低于 0.6%","at":"{blocked_at}"}}}}',
        encoding="utf-8",
    )
    executor = CashCarryExecutor(state)
    settings = BotSettings(manual_confirm_required=False, cash_carry_auto_open_enabled=True, cash_carry_auto_trade_enabled=False)

    result = executor.evaluate_open([_opportunity("XYZUSDT", "3", exchange=ExchangeName.GATE)], settings)

    assert result is not None
    assert result.status == "blocked_by_safety_gate"
    assert "XYZUSDT" in result.steps[2].detail


def test_cash_carry_executor_tracks_multiple_recent_depth_failures(tmp_path) -> None:
    executor = CashCarryExecutor(tmp_path / "state.json")

    executor.state.remember(ExecutionResult("depth-1", "blocked_by_depth", "ABC 深度不足", position=_opportunity("ABCUSDT", "3", exchange=ExchangeName.GATE)))
    executor.state.remember(ExecutionResult("depth-2", "blocked_by_depth", "XYZ 深度不足", position=_opportunity("XYZUSDT", "3", exchange=ExchangeName.GATE)))

    reasons = executor.state.recent_depth_blocked_reasons(executor.depth_block_cooldown_seconds)
    assert (ExchangeName.GATE, "ABCUSDT") in reasons
    assert (ExchangeName.GATE, "XYZUSDT") in reasons


def test_cash_carry_executor_retries_smaller_notional_when_depth_is_thin(tmp_path) -> None:
    state = tmp_path / "state.json"
    executor = _RecordingExecutor(state)
    executor.spot.asks = [[100, 1], [110, 100]]
    executor.swap.bids = [[101, 10000], [90, 1000000000]]
    settings = BotSettings(
        order_notional_usdt=Decimal("300"),
        cash_carry_recovery_probe_notional_usdt=Decimal("100"),
        manual_confirm_required=False,
        cash_carry_auto_open_enabled=True,
        cash_carry_auto_trade_enabled=True,
        cash_carry_auto_transfer_enabled=False,
    )
    item = _opportunity("ABCUSDT", net="2", basis="1", funding="0.01").model_copy(
        update={
            "quantity": Decimal("3"),
            "notional_usdt": Decimal("300"),
            "estimated_open_close_fee": Decimal("0.6"),
            "estimated_net_profit": Decimal("2"),
        }
    )

    result = executor.evaluate_open([item], settings)

    assert result is not None
    assert result.status == "open_submitted"
    assert "深度自适应 100.00U" in result.reason
    assert "单笔名义 100.00" in result.steps[0].detail
    assert executor.spot.orders[0]["amount"] == 1.0
    assert executor.state.load_positions()[0].entry_notional_usdt == Decimal("100.00")


def test_cash_carry_executor_uses_bootstrap_basis_floor_before_v3_samples(tmp_path) -> None:
    executor = CashCarryExecutor(tmp_path / "state.json")
    settings = BotSettings(
        order_notional_usdt=Decimal("300"),
        cash_carry_close_basis_pct=Decimal("0.05"),
        max_slippage_pct=Decimal("0.01"),
    )
    item = _opportunity("ABCUSDT", net="0.95", basis="0.66", funding="0.005").model_copy(
        update={"estimated_open_close_fee": Decimal("0.9")}
    )

    assert executor._open_min_basis_pct(item, settings) == Decimal("0.6")


def test_cash_carry_executor_allows_other_exchange_when_one_exchange_is_active(tmp_path) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        '{"positions":[{"exchange":"BITGET","symbol":"JCTUSDT","base_asset":"JCT","quantity":"90000","spot_entry_price":"0.006","perp_entry_price":"0.0061","opened_at":"2026-06-12T00:00:00+00:00","status":"open"}]}',
        encoding="utf-8",
    )
    executor = CashCarryExecutor(state)
    settings = BotSettings(
        order_notional_usdt=Decimal("500"),
        single_exchange_max_notional_usdt=Decimal("1000"),
        max_total_notional_usdt=Decimal("2000"),
        max_symbol_notional_usdt=Decimal("500"),
        manual_confirm_required=False,
        cash_carry_auto_open_enabled=True,
        cash_carry_auto_trade_enabled=False,
    )

    result = executor.evaluate_open([_opportunity("SKYAIUSDT", "3", exchange=ExchangeName.GATE)], settings)

    assert result is not None
    assert result.status == "blocked_by_safety_gate"
    assert "SKYAIUSDT" in result.steps[2].detail


def test_cash_carry_executor_sets_bitget_isolated_leverage_for_both_sides(tmp_path) -> None:
    exchange = _FakeBitgetLeverage(short_leverage=Decimal("5"))
    result = CashCarryExecutor(tmp_path / "state.json")._set_leverage(exchange, "ABC/USDT:USDT", Decimal("5"), "isolated")

    assert [call["holdSide"] for call in exchange.calls] == ["long", "short"]
    assert result["short"]["leverage"] == 5.0


def test_cash_carry_executor_rejects_bitget_leverage_mismatch(tmp_path) -> None:
    step = ExecutionStep("set_perp_leverage", "done", "设置合约杠杆")

    with pytest.raises(ValueError, match="实际short杠杆"):
        CashCarryExecutor(tmp_path / "state.json")._verify_leverage(
            _FakeBitgetLeverage(short_leverage=Decimal("10")),
            "ABC/USDT:USDT",
            Decimal("5"),
            "short",
            "isolated",
            step,
        )

    assert step.status == "failed"


def test_cash_carry_executor_passes_okx_margin_mode_to_leverage(tmp_path) -> None:
    exchange = _FakeOkxLeverage()

    result = CashCarryExecutor(tmp_path / "state.json")._set_leverage(exchange, "ABC/USDT:USDT", Decimal("5"), "isolated")

    assert result["leverage"]["params"] == {"marginMode": "isolated", "posSide": "net"}


def test_cash_carry_executor_verifies_gate_leverage_from_set_response(tmp_path) -> None:
    step = ExecutionStep("set_perp_leverage", "done", "设置合约杠杆")
    step.raw = {"leverage": {"leverage": "5"}}

    CashCarryExecutor(tmp_path / "state.json")._verify_leverage(
        _FakeGateLeverage(),
        "ABC/USDT:USDT",
        Decimal("5"),
        "short",
        "isolated",
        step,
    )

    assert step.status == "done"


def test_cash_carry_executor_verifies_gate_cross_leverage_limit(tmp_path) -> None:
    step = ExecutionStep("set_perp_leverage", "done", "设置合约杠杆")
    step.raw = {"leverage": {"leverage": "0", "cross_leverage_limit": "5"}}

    CashCarryExecutor(tmp_path / "state.json")._verify_leverage(
        _FakeGateLeverage(),
        "ABC/USDT:USDT",
        Decimal("5"),
        "short",
        "cross",
        step,
    )

    assert step.status == "done"


def test_cash_carry_executor_blocks_when_leverage_cannot_be_verified(tmp_path) -> None:
    step = ExecutionStep("set_perp_leverage", "done", "设置合约杠杆")
    step.raw = {"leverage": {"ok": True}}

    with pytest.raises(ValueError, match="未能确认实际short杠杆"):
        CashCarryExecutor(tmp_path / "state.json")._verify_leverage(
            _FakeGateLeverage(),
            "ABC/USDT:USDT",
            Decimal("5"),
            "short",
            "isolated",
            step,
        )


def test_cash_carry_executor_treats_leverage_not_modified_as_already_set(tmp_path) -> None:
    exchange = _FakeAlreadySetLeverage()
    step = ExecutionStep("set_perp_leverage", "done", "设置合约杠杆")
    executor = CashCarryExecutor(tmp_path / "state.json")

    step.raw = executor._set_leverage(exchange, "ABC/USDT:USDT", Decimal("5"), "isolated")
    executor._verify_leverage(exchange, "ABC/USDT:USDT", Decimal("5"), "short", "isolated", step)

    assert step.status == "done"
    assert step.raw["leverage"]["skipped"] == "already_set"


def test_cash_carry_fixed_usdt_take_profit_has_close_priority(tmp_path) -> None:
    state = _state_with_position(tmp_path)
    executor = CashCarryExecutor(state)
    settings = BotSettings(manual_confirm_required=True, cash_carry_auto_close_enabled=True, take_profit_usdt=Decimal("3"), cash_carry_close_basis_pct=Decimal("0.2"))

    result = executor.evaluate_close([_opportunity(basis="0.9")], settings, [_position_row(net="3.2", basis="0.9")])

    assert result is not None
    assert result.status == "blocked_by_safety_gate"
    assert "固定U止盈达到" in result.steps[0].detail


def test_cash_carry_convergence_closes_when_still_profitable(tmp_path) -> None:
    state = _state_with_position(tmp_path)
    executor = CashCarryExecutor(state)
    settings = BotSettings(manual_confirm_required=True, cash_carry_auto_close_enabled=True, take_profit_usdt=Decimal("8"), cash_carry_close_basis_pct=Decimal("0.2"))

    result = executor.evaluate_close([_opportunity(basis="0.1")], settings, [_position_row(net="0.8", basis="0.1")])

    assert result is not None
    assert result.status == "blocked_by_safety_gate"
    assert "执行前净利估算" in result.steps[0].detail


def test_cash_carry_v3_turnover_closes_profitable_position_before_full_convergence(tmp_path) -> None:
    state = _state_with_position(tmp_path)
    executor = _RecordingExecutor(state)
    settings = BotSettings(
        manual_confirm_required=False,
        cash_carry_auto_close_enabled=True,
        take_profit_usdt=Decimal("3"),
        order_notional_usdt=Decimal("100"),
    )

    result = executor.evaluate_close([_opportunity(basis="0.9")], settings, [_position_row(net="0.8", basis="0.9")])

    assert result is not None
    assert result.status == "close_submitted"
    assert "V3周转止盈达到" in result.reason


def test_cash_carry_v3_turnover_waits_until_profit_covers_execution_buffer(tmp_path) -> None:
    state = _state_with_position(tmp_path)
    executor = _RecordingExecutor(state)
    settings = BotSettings(
        manual_confirm_required=False,
        cash_carry_auto_close_enabled=True,
        take_profit_usdt=Decimal("3"),
        order_notional_usdt=Decimal("100"),
    )

    result = executor.evaluate_close([_opportunity(basis="0.9")], settings, [_position_row(net="0.4", basis="0.9")])

    assert result is None
    assert executor.spot.orders == []
    assert executor.swap.orders == []


def test_cash_carry_convergence_waits_when_loss_can_recover_from_funding(tmp_path) -> None:
    state = _state_with_position(tmp_path)
    executor = CashCarryExecutor(state)
    settings = BotSettings(
        manual_confirm_required=True,
        cash_carry_auto_close_enabled=True,
        stop_loss_usdt=Decimal("5"),
        cash_carry_close_basis_pct=Decimal("0.2"),
        cash_carry_max_recovery_funding_intervals=Decimal("100"),
    )

    result = executor.evaluate_close([_opportunity(basis="0.1")], settings, [_position_row(net="-1.2", basis="0.1", funding="0.02")])

    assert result is None


def test_cash_carry_convergence_waits_on_loss_without_replacement(tmp_path) -> None:
    state = _state_with_position(tmp_path)
    executor = _RecordingExecutor(state)
    settings = BotSettings(manual_confirm_required=True, cash_carry_auto_close_enabled=True, stop_loss_usdt=Decimal("5"), cash_carry_close_basis_pct=Decimal("0.2"))

    result = executor.evaluate_close([_opportunity(basis="0.1", funding="0")], settings, [_position_row(net="-1.2", basis="0.1", funding="0")])

    assert result is None
    assert executor.spot.orders == []
    assert executor.swap.orders == []


def test_cash_carry_convergence_keeps_loss_above_recovery_release_cap(tmp_path) -> None:
    state = _state_with_position(tmp_path)
    executor = CashCarryExecutor(state)
    settings = BotSettings(
        manual_confirm_required=True,
        cash_carry_auto_close_enabled=True,
        stop_loss_usdt=Decimal("20"),
        cash_carry_close_basis_pct=Decimal("0.2"),
        cash_carry_recovery_exit_max_loss_usdt=Decimal("6"),
    )

    result = executor.evaluate_close([_opportunity(basis="0.1", funding="0")], settings, [_position_row(net="-6.2", basis="0.1", funding="0")])

    assert result is None


def test_cash_carry_v3_releases_stale_small_loss_only_when_replacement_exists(tmp_path) -> None:
    state = _state_with_position(tmp_path)
    executor = _RecordingExecutor(state)
    settings = BotSettings(
        manual_confirm_required=False,
        cash_carry_auto_close_enabled=True,
        order_notional_usdt=Decimal("100"),
        take_profit_usdt=Decimal("3"),
        cash_carry_close_basis_pct=Decimal("0.2"),
    )
    replacement = _opportunity("XYZUSDT", net="2.0", basis="1.2", funding="0.02").model_copy(
        update={"blocked_reasons": ["同交易所已有正向期现持仓，按一所一币规则禁止开仓"]}
    )

    result = executor.evaluate_close([replacement], settings, [_position_row(net="-0.8", basis="0.6", funding="0")])

    assert result is not None
    assert result.status == "close_submitted"
    assert "V3死仓释放" in result.reason
    assert "XYZUSDT" in result.reason


def test_cash_carry_v3_dead_release_waits_without_replacement(tmp_path) -> None:
    state = _state_with_position(tmp_path)
    executor = _RecordingExecutor(state)
    settings = BotSettings(
        manual_confirm_required=False,
        cash_carry_auto_close_enabled=True,
        order_notional_usdt=Decimal("100"),
        take_profit_usdt=Decimal("3"),
        cash_carry_close_basis_pct=Decimal("0.2"),
    )

    result = executor.evaluate_close([], settings, [_position_row(net="-0.8", basis="0.6", funding="0")])

    assert result is None
    assert executor.spot.orders == []
    assert executor.swap.orders == []


def test_cash_carry_v3_switches_slow_funding_recovery_when_replacement_covers_cost(tmp_path) -> None:
    state = _state_with_position(tmp_path)
    executor = _RecordingExecutor(state)
    settings = BotSettings(
        manual_confirm_required=False,
        cash_carry_auto_close_enabled=True,
        order_notional_usdt=Decimal("100"),
        take_profit_usdt=Decimal("3"),
        cash_carry_close_basis_pct=Decimal("0.2"),
        cash_carry_target_daily_trades=10,
    )
    replacement = _opportunity("XYZUSDT", net="1.5", basis="1.2", funding="0.02").model_copy(
        update={"blocked_reasons": ["同交易所已有正向期现持仓，按一所一币规则禁止开仓"]}
    )

    result = executor.evaluate_close([replacement], settings, [_position_row(net="-0.8", basis="0.6", funding="0.1")])

    assert result is not None
    assert result.status == "close_submitted"
    assert "V3低效仓位切换" in result.reason
    assert "超过周转上限" in result.reason
    assert "XYZUSDT" in result.reason


def test_cash_carry_v3_keeps_slow_funding_recovery_when_replacement_is_too_thin(tmp_path) -> None:
    state = _state_with_position(tmp_path)
    executor = _RecordingExecutor(state)
    settings = BotSettings(
        manual_confirm_required=False,
        cash_carry_auto_close_enabled=True,
        order_notional_usdt=Decimal("100"),
        take_profit_usdt=Decimal("3"),
        cash_carry_close_basis_pct=Decimal("0.2"),
        cash_carry_target_daily_trades=10,
    )
    replacement = _opportunity("XYZUSDT", net="1.35", basis="1.2", funding="0.02").model_copy(
        update={"blocked_reasons": ["同交易所已有正向期现持仓，按一所一币规则禁止开仓"]}
    )

    result = executor.evaluate_close([replacement], settings, [_position_row(net="-0.8", basis="0.6", funding="0.1")])

    assert result is None
    assert executor.spot.orders == []
    assert executor.swap.orders == []


def test_cash_carry_v3_dead_release_blocks_large_loss(tmp_path) -> None:
    state = _state_with_position(tmp_path)
    executor = _RecordingExecutor(state)
    settings = BotSettings(
        manual_confirm_required=False,
        cash_carry_auto_close_enabled=True,
        order_notional_usdt=Decimal("100"),
        take_profit_usdt=Decimal("3"),
        cash_carry_close_basis_pct=Decimal("0.2"),
    )

    result = executor.evaluate_close([_opportunity("XYZUSDT", net="5.0")], settings, [_position_row(net="-1.2", basis="0.6", funding="0")])

    assert result is None
    assert executor.spot.orders == []
    assert executor.swap.orders == []


def test_cash_carry_fixed_usdt_stop_loss_can_close_loss(tmp_path) -> None:
    state = _state_with_position(tmp_path)
    executor = CashCarryExecutor(state)
    settings = BotSettings(manual_confirm_required=True, cash_carry_auto_close_enabled=True, stop_loss_usdt=Decimal("5"), cash_carry_close_basis_pct=Decimal("0.2"))

    result = executor.evaluate_close([_opportunity(basis="0.1", funding="0")], settings, [_position_row(net="-5.2", basis="0.1", funding="0")])

    assert result is not None
    assert result.status == "blocked_by_safety_gate"
    assert "固定U止损达到" in result.steps[0].detail


def test_cash_carry_close_uses_live_matched_quantities_instead_of_stale_state(tmp_path) -> None:
    state = _state_with_position(tmp_path)
    executor = _RecordingExecutor(state)
    settings = BotSettings(manual_confirm_required=False, cash_carry_auto_close_enabled=True, take_profit_usdt=Decimal("3"))
    live = _position_row(net="3.2", basis="0.9")
    live.spot_quantity = Decimal("1.97")
    live.perp_base_quantity = Decimal("1.98")
    live.quantity_gap = Decimal("-0.01")

    result = executor.evaluate_close([_opportunity(basis="0.9")], settings, [live])

    assert result is not None
    assert result.status == "close_submitted"
    assert executor.spot.orders[0]["amount"] == 1.97
    assert executor.swap.orders[0]["amount"] == 19800.0


def test_cash_carry_mismatch_sells_excess_spot_before_close(tmp_path) -> None:
    state = _state_with_position(tmp_path, status="open")
    executor = _RecordingExecutor(state)
    settings = BotSettings(manual_confirm_required=True, cash_carry_auto_close_enabled=True)
    live = _position_row(net="3.2", basis="0.9")
    live.status = "mismatch"
    live.spot_quantity = Decimal("1.05")
    live.perp_base_quantity = Decimal("1")
    live.quantity_gap = Decimal("0.05")

    result = executor.evaluate_close([_opportunity(basis="0.9")], settings, [live])

    assert result is not None
    assert result.status == "rebalance_submitted"
    assert executor.spot.orders == [{"id": "spot-close", "symbol": "ABC/USDT", "side": "sell", "amount": 0.05}]
    assert executor.swap.orders == []
    position = executor.state.load_positions(include_non_open=True)[0]
    assert position.status == "open"
    assert position.quantity == Decimal("1")


def test_cash_carry_mismatch_reduces_excess_perp_before_close(tmp_path) -> None:
    state = _state_with_position(tmp_path, status="open")
    executor = _RecordingExecutor(state)
    settings = BotSettings(manual_confirm_required=True, cash_carry_auto_close_enabled=True)
    live = _position_row(net="3.2", basis="0.9")
    live.status = "mismatch"
    live.spot_quantity = Decimal("1")
    live.perp_base_quantity = Decimal("1.05")
    live.quantity_gap = Decimal("-0.05")

    result = executor.evaluate_close([_opportunity(basis="0.9")], settings, [live])

    assert result is not None
    assert result.status == "rebalance_submitted"
    assert executor.spot.orders == []
    assert executor.swap.orders == [{"id": "swap-close", "symbol": "ABC/USDT:USDT", "side": "buy", "amount": 500.0, "params": {"reduceOnly": True}}]
    position = executor.state.load_positions(include_non_open=True)[0]
    assert position.status == "open"
    assert position.quantity == Decimal("1")


def test_cash_carry_fixed_take_profit_requires_depth_profit_above_target(tmp_path) -> None:
    state = _state_with_position(tmp_path)
    executor = _RecordingExecutor(state)
    executor.spot.bids = [[101, 100]]
    executor.swap.asks = [[100.5, 100000]]
    settings = BotSettings(manual_confirm_required=False, cash_carry_auto_close_enabled=True, take_profit_usdt=Decimal("3"))

    result = executor.evaluate_close([_opportunity(basis="0.9")], settings, [_position_row(net="3.2", basis="0.9")])

    assert result is not None
    assert result.status == "blocked_by_depth"
    assert "盘口可成交净利" in result.reason
    assert "低于平仓安全垫 3.5000 USDT" in result.reason
    assert executor.spot.orders == []
    assert executor.swap.orders == []


def test_cash_carry_close_guard_uses_live_perp_entry_price(tmp_path) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        '{"positions":[{"id":"pos-1","exchange":"GATE","symbol":"ABCUSDT","base_asset":"ABC","quantity":"1","spot_entry_price":"100","perp_entry_price":"98","spot_order_id":"s1","perp_order_id":"p1","opened_at":"2026-06-09T00:00:00+00:00","status":"open"}]}',
        encoding="utf-8",
    )
    executor = _RecordingExecutor(state)
    executor.spot.bids = [[100, 100]]
    executor.swap.asks = [[96.8, 100000]]
    settings = BotSettings(manual_confirm_required=False, cash_carry_auto_close_enabled=True, take_profit_usdt=Decimal("3"))
    live = _position_row(net="4.2", basis="0.9")
    live.perp_entry_price = Decimal("101")

    result = executor.evaluate_close([_opportunity(basis="0.9")], settings, [live])

    assert result is not None
    assert result.status == "close_submitted"
    assert executor.spot.orders == [{"id": "spot-close", "symbol": "ABC/USDT", "side": "sell", "amount": 1.0}]
    assert executor.swap.orders == [{"id": "swap-close", "symbol": "ABC/USDT:USDT", "side": "buy", "amount": 10000.0, "params": {"reduceOnly": True}}]


def test_cash_carry_close_blocks_when_depth_guard_estimates_loss(tmp_path) -> None:
    state = _state_with_position(tmp_path)
    executor = _RecordingExecutor(state)
    executor.spot.bids = [[99.9, 100]]
    executor.swap.asks = [[101.2, 100000]]
    settings = BotSettings(manual_confirm_required=False, cash_carry_auto_close_enabled=True, cash_carry_close_basis_pct=Decimal("0.2"))

    result = executor.evaluate_close([_opportunity(basis="0.1")], settings, [_position_row(net="0.8", basis="0.1")])

    assert result is not None
    assert result.status == "blocked_by_depth"
    assert "盘口可成交净利" in result.reason
    assert executor.spot.orders == []
    assert executor.swap.orders == []


def test_cash_carry_loss_exit_waits_without_replacement(tmp_path) -> None:
    state = _state_with_position(tmp_path)
    executor = _RecordingExecutor(state)
    executor.spot.bids = [[99.9, 100]]
    executor.swap.asks = [[101.2, 100000]]
    settings = BotSettings(manual_confirm_required=False, cash_carry_auto_close_enabled=True, cash_carry_close_basis_pct=Decimal("0.2"))

    result = executor.evaluate_close([_opportunity(basis="0.1", funding="0")], settings, [_position_row(net="-1.2", basis="0.1", funding="0")])

    assert result is None
    assert executor.spot.orders == []
    assert executor.swap.orders == []


def test_cash_carry_legacy_converged_small_loss_releases_slot_without_replacement(tmp_path) -> None:
    state = _state_with_position(tmp_path, status="open")
    executor = _RecordingExecutor(state)
    settings = BotSettings(
        manual_confirm_required=False,
        cash_carry_auto_close_enabled=True,
        cash_carry_close_basis_pct=Decimal("0.2"),
        order_notional_usdt=Decimal("100"),
        take_profit_usdt=Decimal("3"),
        cash_carry_target_daily_trades=10,
    )

    result = executor.evaluate_close([], settings, [_position_row(net="-0.8", basis="0.1", funding="0.1")])

    assert result is not None
    assert result.status == "close_submitted"
    assert "旧规则低效仓位释放" in result.reason
    assert executor.spot.orders == [{"id": "spot-close", "symbol": "ABC/USDT", "side": "sell", "amount": 1.0}]
    assert executor.swap.orders == [{"id": "swap-close", "symbol": "ABC/USDT:USDT", "side": "buy", "amount": 10000.0, "params": {"reduceOnly": True}}]


def test_cash_carry_v3_converged_small_loss_waits_without_replacement(tmp_path) -> None:
    state = _state_with_position(tmp_path, status="open", strategy_version=CASH_CARRY_RULESET_VERSION)
    executor = _RecordingExecutor(state)
    settings = BotSettings(
        manual_confirm_required=False,
        cash_carry_auto_close_enabled=True,
        cash_carry_close_basis_pct=Decimal("0.2"),
        order_notional_usdt=Decimal("100"),
        take_profit_usdt=Decimal("3"),
        cash_carry_target_daily_trades=10,
    )

    result = executor.evaluate_close([], settings, [_position_row(net="-0.8", basis="0.1", funding="0.1")])

    assert result is None
    assert executor.spot.orders == []
    assert executor.swap.orders == []


def test_cash_carry_loss_exit_switches_only_when_replacement_covers_loss(tmp_path) -> None:
    state = _state_with_position(tmp_path)
    executor = _RecordingExecutor(state)
    executor.spot.bids = [[99.9, 100]]
    executor.swap.asks = [[101.2, 100000]]
    settings = BotSettings(
        manual_confirm_required=False,
        cash_carry_auto_close_enabled=True,
        cash_carry_close_basis_pct=Decimal("0.2"),
        order_notional_usdt=Decimal("100"),
    )
    replacement = _opportunity("XYZUSDT", net="2.0", basis="1.2", funding="0.02").model_copy(
        update={"blocked_reasons": ["同交易所已有正向期现持仓，按一所一币规则禁止开仓"]}
    )

    result = executor.evaluate_close([replacement], settings, [_position_row(net="-1.2", basis="0.1", funding="0")])

    assert result is not None
    assert result.status == "close_submitted"
    assert "V3亏损切换" in result.reason
    assert "XYZUSDT" in result.reason
    assert executor.spot.orders == [{"id": "spot-close", "symbol": "ABC/USDT", "side": "sell", "amount": 1.0}]
    assert executor.swap.orders == [{"id": "swap-close", "symbol": "ABC/USDT:USDT", "side": "buy", "amount": 10000.0, "params": {"reduceOnly": True}}]


def test_gate_transfer_is_skipped_when_spot_and_swap_have_enough_usdt(tmp_path) -> None:
    executor = CashCarryExecutor(tmp_path / "state.json")
    exchange = _FakeGate(spot_free=Decimal("940"), swap_free=Decimal("940"))
    settings = BotSettings(order_notional_usdt=Decimal("500"), default_leverage=Decimal("5"), cash_carry_auto_transfer_enabled=True)
    step = executor._open_plan(_opportunity(), settings)[0]

    executor._maybe_transfer(exchange, _opportunity(), settings, step)

    assert step.status == "skipped"
    assert exchange.transfers == []
    assert "余额充足" in step.detail


def test_gate_transfer_requires_enough_spot_usdt_for_unified_account(tmp_path) -> None:
    executor = CashCarryExecutor(tmp_path / "state.json")
    exchange = _FakeGate(spot_free=Decimal("499"), swap_free=Decimal("1000"))
    settings = BotSettings(order_notional_usdt=Decimal("500"), default_leverage=Decimal("5"), cash_carry_auto_transfer_enabled=True)
    step = executor._open_plan(_opportunity(), settings)[0]

    try:
        executor._maybe_transfer(exchange, _opportunity(), settings, step)
    except ValueError as exc:
        assert "GATE 现货 USDT 可用余额不足" in str(exc)
    else:
        raise AssertionError("expected insufficient spot USDT error")


def test_contract_order_amount_converts_base_quantity_to_contracts() -> None:
    assert contract_order_amount(_FakeSwap(is_pre_market=False), "BTC/USDT:USDT", Decimal("0.03265413")) == 326.0


def _opportunity(symbol: str = "ABCUSDT", net: str = "0.8", basis: str = "1", funding: str = "0.01", exchange: ExchangeName = ExchangeName.GATE) -> CashCarryOpportunity:
    return CashCarryOpportunity(
        exchange=exchange,
        symbol=symbol,
        spot_price=Decimal("100"),
        perp_price=Decimal("101"),
        basis_pct=Decimal(basis),
        funding_rate_pct=Decimal(funding),
        quantity=Decimal("1"),
        spot_volume_24h_usdt=Decimal("1000000"),
        perp_volume_24h_usdt=Decimal("1000000"),
        estimated_basis_profit=Decimal("1"),
        estimated_funding_income=Decimal("0.01"),
        estimated_open_close_fee=Decimal("0.2"),
        estimated_net_profit=Decimal(net),
        blocked_reasons=[],
        data_source=DataSource.LIVE,
        updated_at=datetime.now(timezone.utc),
    )


def _position_row(net: str, basis: str, funding: str = "0.01") -> CashCarryPositionRow:
    return CashCarryPositionRow(
        exchange=ExchangeName.GATE,
        symbol="ABCUSDT",
        status="matched",
        spot_quantity=Decimal("1"),
        spot_entry_price=Decimal("100"),
        spot_price=Decimal("100"),
        spot_unrealized_pnl=Decimal("0"),
        perp_side="short",
        perp_contracts=Decimal("1"),
        perp_base_quantity=Decimal("1"),
        contract_size=Decimal("1"),
        perp_entry_price=Decimal("101"),
        perp_mark_price=Decimal("101"),
        leverage=Decimal("2"),
        perp_unrealized_pnl=Decimal("0"),
        estimated_funding_rate_pct=Decimal(funding),
        estimated_funding_income=Decimal("0"),
        estimated_open_fee=Decimal("0.1"),
        estimated_close_fee=Decimal("0.1"),
        current_net_profit=Decimal(net),
        quantity_gap=Decimal("0"),
        basis_pct=Decimal(basis),
        updated_at=datetime.now(timezone.utc),
    )


def _state_with_position(tmp_path, status: str = "mismatch", strategy_version: str | None = None):
    state = tmp_path / "state.json"
    version = f', "strategy_version":"{strategy_version}"' if strategy_version else ""
    state.write_text(
        f'{{"positions":[{{"id":"pos-1","exchange":"GATE","symbol":"ABCUSDT","base_asset":"ABC","quantity":"1","spot_entry_price":"100","perp_entry_price":"101","spot_order_id":"s1","perp_order_id":"p1","opened_at":"2026-06-09T00:00:00+00:00","status":"{status}"{version}}}]}}',
        encoding="utf-8",
    )
    return state


class _FakeBitgetLeverage:
    id = "bitget"

    def __init__(self, short_leverage: Decimal) -> None:
        self.short_leverage = short_leverage
        self.calls = []

    def set_leverage(self, leverage, symbol, params=None):
        call = {"leverage": leverage, "symbol": symbol, **(params or {})}
        self.calls.append(call)
        return call

    def fetch_leverage(self, symbol):
        return {"symbol": symbol, "shortLeverage": self.short_leverage, "longLeverage": Decimal("5")}


class _FakeOkxLeverage:
    id = "okx"

    def set_leverage(self, leverage, symbol, params=None):
        return {"leverage": leverage, "symbol": symbol, "params": params or {}}


class _FakeGateLeverage:
    id = "gateio"

    def set_leverage(self, leverage, symbol, params=None):
        return {"leverage": str(leverage), "symbol": symbol, "params": params or {}}


class _FakeAlreadySetLeverage:
    id = "bybit"

    def set_leverage(self, leverage, symbol, params=None):
        raise ValueError('bybit {"retCode":110043,"retMsg":"leverage not modified"}')

    def fetch_leverage(self, symbol):
        return {"symbol": symbol, "longLeverage": Decimal("5"), "shortLeverage": Decimal("5")}


class _FakeGate:
    id = "gateio"

    def __init__(self, spot_free: Decimal, swap_free: Decimal) -> None:
        self.spot_free = spot_free
        self.swap_free = swap_free
        self.transfers = []

    def fetch_balance(self, params):
        free = self.spot_free if params["type"] == "spot" else self.swap_free
        return {"USDT": {"free": str(free)}}

    def transfer(self, code, amount, from_account, to_account):
        self.transfers.append((code, amount, from_account, to_account))
        return {"ok": True}


class _FakeSwap:
    id = "gateio"

    def __init__(self, is_pre_market: bool = False) -> None:
        self.is_pre_market = is_pre_market

    def load_markets(self):
        return None

    def market(self, symbol):
        return {"contractSize": "0.0001", "info": {"is_pre_market": self.is_pre_market}}

    def amount_to_precision(self, symbol, amount):
        return str(int(amount))

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        return {"id": "swap-close", "amount": amount, "params": params}


class _FakeSpot:
    has = {"fetchOrder": False}


class _RecordingSpot(_FakeSpot):
    def __init__(self) -> None:
        self.orders = []
        self.bids = [[103, 100]]
        self.asks = [[104, 100]]

    def fetch_order_book(self, symbol, limit=20):
        return {"asks": self.asks, "bids": self.bids}

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        order = {"id": "spot-close", "symbol": symbol, "side": side, "amount": amount}
        self.orders.append(order)
        return order


class _RecordingSwap(_FakeSwap):
    def __init__(self) -> None:
        super().__init__()
        self.orders = []
        self.asks = [[100, 100000]]
        self.bids = [[99, 100000]]

    def fetch_order_book(self, symbol, limit=20):
        return {"asks": self.asks, "bids": self.bids}

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        order = {"id": "swap-close", "symbol": symbol, "side": side, "amount": amount, "params": params}
        self.orders.append(order)
        return order


class _RecordingExecutor(CashCarryExecutor):
    def __init__(self, state_path) -> None:
        super().__init__(state_path)
        self.spot = _RecordingSpot()
        self.swap = _RecordingSwap()

    def _exchange(self, exchange_name, default_type):
        return self.spot if default_type == "spot" else self.swap

    def _safety_gate(self, settings, opening, protective=False):
        return []
