import threading
import time
from datetime import datetime, timezone
from decimal import Decimal

from app.core.models import (
    BotSettings,
    CashCarryPositionRow,
    DataSource,
    ExchangeBalance,
    ExchangeName,
    PositionSnapshot,
    RealtimeSnapshot,
    RiskEvent,
    TradeHistory,
)
from app.core.env import ai_status, credential_statuses, env_bool
from app.services.ai_monitor import DeepSeekMonitor
from app.services.cash_carry_positions import CashCarryPositionBuilder
from app.services.cash_carry_scope import CASH_CARRY_EXCHANGES
from app.services.cash_carry_scanner import CashCarryScanner
from app.services.execution_state import recent_execution_results
from app.services.live_read import LiveReadService
from app.services.live_runtime import LiveRuntimeCache
from app.services.mt4_bridge import Mt4QuoteStore, Mt4SpreadScanner
from app.services.settings_store import SettingsStore
from app.services.trade_history_store import TradeHistoryStore
from app.services.ws_ticker_cache import WSTickerCache


class ArbitrageEngine:
    def __init__(self, settings_store: SettingsStore) -> None:
        self.settings_store = settings_store
        self.live_read = LiveReadService()
        self.ticker_cache = WSTickerCache()
        self.cash_carry_scanner = CashCarryScanner()
        self.cash_carry_positions = CashCarryPositionBuilder(self.ticker_cache)
        self.mt4_quote_store = Mt4QuoteStore()
        self.mt4_spread_scanner = Mt4SpreadScanner(self.mt4_quote_store)
        self.trade_history = TradeHistoryStore()
        self.ai_monitor = DeepSeekMonitor()
        self._cash_positions_cache: list[CashCarryPositionRow] = []
        self._cash_positions_cache_at = 0.0
        self._cash_positions_refreshing = False
        self._cash_positions_lock = threading.Lock()
        self.live_runtime = LiveRuntimeCache(
            self.live_read,
            self.cash_carry_scanner,
            self.mt4_spread_scanner,
            ticker_cache=self.ticker_cache,
        )

    def snapshot(self) -> RealtimeSnapshot:
        settings = self.settings_store.load()
        live_enabled = self.live_read.live_data_enabled()
        live_runtime = self.live_runtime.get(settings) if live_enabled else None
        balances = live_runtime.account.balances if live_runtime else self.get_balances()
        positions = live_runtime.account.positions if live_runtime else self.get_positions(settings)
        cash_prices = (live_runtime.cash_carry.opportunities + live_runtime.cash_carry.candidates) if live_runtime else []
        cash_opps = live_runtime.cash_carry.opportunities if live_runtime else []
        cash_candidates = live_runtime.cash_carry.candidates if live_runtime else []
        mt4_opps = live_runtime.mt4_spread_opportunities if live_runtime else []
        mt4_candidates = live_runtime.mt4_spread_candidates if live_runtime else []
        cash_positions = self._cash_positions_snapshot(positions, cash_prices, settings) if live_enabled else []
        risk_events = self.get_risk_events(
            settings,
            live_runtime.account.issues if live_runtime else [],
            live_runtime.cash_carry.issues if live_runtime else [],
            live_runtime.mt4_spread_issues if live_runtime else [],
            cash_positions,
        )
        return RealtimeSnapshot(
            balances=balances,
            positions=positions,
            cash_carry_opportunities=cash_opps,
            cash_carry_candidates=cash_candidates,
            cash_carry_positions=cash_positions,
            mt4_spread_opportunities=mt4_opps,
            mt4_spread_candidates=mt4_candidates,
            trades=self.get_trades(),
            settings=settings,
            risk_events=risk_events,
            credential_status=credential_statuses(),
            ai_insight=self.ai_monitor.insight(
                balances,
                positions,
                risk_events,
                settings.ai_risk_monitor_enabled,
                cash_positions,
                cash_opps,
                cash_candidates,
                self._strategy_switches(settings),
            ),
            data_source=DataSource.LIVE if live_enabled else DataSource.MOCK,
        )

    def get_balances(self) -> list[ExchangeBalance]:
        now = datetime.now(timezone.utc)
        return [
            ExchangeBalance(exchange=ExchangeName.GATE, equity_usdt=Decimal("2600"), available_usdt=Decimal("2450"), margin_used_usdt=Decimal("150"), updated_at=now),
            ExchangeBalance(exchange=ExchangeName.BITGET, equity_usdt=Decimal("3100"), available_usdt=Decimal("3020"), margin_used_usdt=Decimal("80"), updated_at=now),
        ]

    def get_positions(self, settings: BotSettings) -> list[PositionSnapshot]:
        return []

    def get_trades(self) -> list[TradeHistory]:
        return self.trade_history.load()

    def _cash_positions_snapshot(self, positions: list[PositionSnapshot], cash_prices: list, settings: BotSettings) -> list[CashCarryPositionRow]:
        if not positions and not self.cash_carry_positions.has_open_state_records():
            with self._cash_positions_lock:
                self._cash_positions_cache = []
                self._cash_positions_cache_at = time.monotonic()
            return []
        with self._cash_positions_lock:
            cached = list(self._cash_positions_cache)
            stale = time.monotonic() - self._cash_positions_cache_at > 5
            if stale and not self._cash_positions_refreshing:
                self._cash_positions_refreshing = True
                threading.Thread(
                    target=self._refresh_cash_positions,
                    args=(list(positions), list(cash_prices), settings),
                    daemon=True,
                    name="cash-position-refresh",
                ).start()
            return cached

    def _refresh_cash_positions(self, positions: list[PositionSnapshot], cash_prices: list, settings: BotSettings) -> None:
        try:
            rows = self.cash_carry_positions.build(positions, cash_prices, settings)
            rows = [item for item in rows if ExchangeName(item.exchange) in CASH_CARRY_EXCHANGES]
        except Exception:
            rows = []
        with self._cash_positions_lock:
            self._cash_positions_cache = rows
            self._cash_positions_cache_at = time.monotonic()
            self._cash_positions_refreshing = False

    def get_risk_events(
        self,
        settings: BotSettings,
        live_issues: list[str] | None = None,
        cash_carry_issues: list[str] | None = None,
        mt4_spread_issues: list[str] | None = None,
        cash_carry_positions: list[CashCarryPositionRow] | None = None,
    ) -> list[RiskEvent]:
        now = datetime.now(timezone.utc)
        events = []
        if self.live_read.live_data_enabled():
            if env_bool("TRADING_ENABLED") and env_bool("ORDER_EXECUTION_ENABLED") and not env_bool("API_READ_ONLY_MODE", default=True):
                events.append(RiskEvent(id="live-trading-enabled", severity="warning", title="实盘交易模式已开启", detail="真实账户读取、交易总开关和下单执行总开关均已开启。策略仍会继续校验子开关、人工确认、交易所权限和接口返回。", action="保持小额参数，优先处理执行器失败原因。", created_at=now))
            else:
                events.append(RiskEvent(id="live-read-only", severity="info", title="实盘只读模式", detail="正在读取真实账户数据，但交易或下单执行总开关仍关闭。", action="检查 .env 的 TRADING_ENABLED、ORDER_EXECUTION_ENABLED 和 API_READ_ONLY_MODE。", created_at=now))
            events.append(RiskEvent(id="execution-checks-enabled", severity="info", title="执行校验已开启", detail="机会排行已过滤双向充值/提现链路和两边盘口深度，价格快刷新会在两次全量校验之间更新价差。", action="实盘前仍需小额验证成交回报和链路状态。", created_at=now))
        else:
            events.append(RiskEvent(id="auto-open-disabled", severity="info", title="自动开仓关闭", detail="当前只允许监控和人工确认，实盘自动开仓未启用。", action="需要实盘前在参数设置中手动开启。", created_at=now))
        for index, issue in enumerate(live_issues or []):
            events.append(RiskEvent(id=f"live-issue-{index}", severity="warning", title="交易所只读接口异常", detail=issue, action="检查 API 权限、IP 白名单、账户类型和交易所服务状态。", created_at=now))
        for index, issue in enumerate(cash_carry_issues or []):
            events.append(RiskEvent(id=f"cash-carry-issue-{index}", severity="warning", title="期现扫描接口异常", detail=issue, action="检查同所现货、合约行情和资金费率接口。", created_at=now))
        for index, issue in enumerate(mt4_spread_issues or []):
            events.append(RiskEvent(id=f"mt4-spread-issue-{index}", severity="warning", title="MT4 价差扫描异常", detail=issue, action="检查 MT4 插件报价推送、品种映射和交易所合约行情接口。", created_at=now))
        events.extend(self._cash_carry_add_config_events(settings, now))
        for result in recent_execution_results():
            if result["status"] not in {"failed", "blocked_by_safety_gate"}:
                continue
            if self._stale_execution_result(result, cash_carry_positions or []):
                continue
            events.append(RiskEvent(id=f"execution-{result['strategy_id']}", severity="warning", title=f"{result['title']}未完成", detail=self._friendly_execution_reason(result["reason"]), action="按失败原因补齐交易所 API 权限、账户资金或关闭对应自动步骤后再重试。", created_at=now))
        ai = ai_status()
        if ai["provider"] == "deepseek" and not ai["configured"]:
            events.append(RiskEvent(id="deepseek-missing-key", severity="info", title="DeepSeek 未配置", detail="DeepSeek API key 还没有配置。", action="在 API 管理页面保存 DeepSeek API key 后即可接入 AI 风险监控。", created_at=now))
        if settings.emergency_close_enabled:
            events.append(RiskEvent(id="emergency-close", severity="critical", title="紧急平仓开关已打开", detail="系统应停止新开仓并准备执行保护性平仓。", action="检查持仓并人工确认。", created_at=now))
        return events

    def _cash_carry_add_config_events(self, settings: BotSettings, now: datetime) -> list[RiskEvent]:
        if (
            not settings.cash_carry_enabled
            or not settings.cash_carry_auto_open_enabled
            or settings.max_add_count <= 0
            or settings.add_trigger_spread_pct <= 0
            or settings.order_notional_usdt <= 0
            or settings.add_notional_usdt <= 0
        ):
            return []
        first_add_required = settings.order_notional_usdt + settings.add_notional_usdt
        reasons = []
        if settings.max_symbol_notional_usdt < first_add_required:
            reasons.append(f"单币最大仓位 {settings.max_symbol_notional_usdt}U 小于首仓+一次补仓所需 {first_add_required}U")
        if settings.single_exchange_max_notional_usdt < first_add_required:
            reasons.append(f"单所最大暴露 {settings.single_exchange_max_notional_usdt}U 小于首仓+一次补仓所需 {first_add_required}U")
        if settings.max_total_notional_usdt < first_add_required:
            reasons.append(f"最大总仓位 {settings.max_total_notional_usdt}U 小于首仓+一次补仓所需 {first_add_required}U")
        if not reasons:
            return []
        return [
            RiskEvent(
                id="cash-carry-add-config-blocked",
                severity="warning",
                title="正向期现补仓参数不可执行",
                detail="；".join(reasons),
                action="若需要补仓，调高对应仓位上限或降低单笔下单金额；否则系统只会持有首仓并等待平仓/止损。",
                created_at=now,
            )
        ]

    def _stale_execution_result(self, result: dict[str, str], rows: list[CashCarryPositionRow]) -> bool:
        return self._stale_cash_carry_result(result, rows)

    def _stale_cash_carry_result(self, result: dict[str, str], rows: list[CashCarryPositionRow]) -> bool:
        if result.get("strategy_id") != "cash-carry":
            return False
        reason = result.get("reason", "")
        for row in rows:
            exchange = row.exchange.value if hasattr(row.exchange, "value") else str(row.exchange)
            if row.status == "matched" and exchange in reason and row.symbol in reason:
                return True
        return False

    def _friendly_execution_reason(self, reason: str) -> str:
        text = reason.lower()
        if "10006" in text or "too many visits" in text or "rate limit" in text:
            return "交易所 API 限频，系统已短暂冷却并等待自动重试。"
        if "110043" in text or "leverage not modified" in text:
            return "交易所提示杠杆已是目标值，系统会继续做实际杠杆校验。"
        return reason

    def _strategy_switches(self, settings: BotSettings) -> dict[str, object]:
        switches = {
            "cash_carry_auto_open": settings.cash_carry_auto_open_enabled,
            "cash_carry_auto_trade": settings.cash_carry_auto_trade_enabled,
            "cash_carry_auto_close": settings.cash_carry_auto_close_enabled,
            "mt4_spread_enabled": settings.mt4_spread_enabled,
            "manual_confirm_required": settings.manual_confirm_required,
        }
        return {
            "enabled": [key for key, value in switches.items() if value],
            "disabled": [key for key, value in switches.items() if not value],
            "params": {
                "cash_carry_min_basis_pct": str(settings.cash_carry_min_basis_pct),
                "cash_carry_close_basis_pct": str(settings.cash_carry_close_basis_pct),
                "take_profit_usdt": str(settings.take_profit_usdt),
                "stop_loss_usdt": str(settings.stop_loss_usdt),
                "add_notional_usdt": str(settings.add_notional_usdt),
            },
        }

    def update_settings(self, settings: BotSettings) -> BotSettings:
        return self.settings_store.save(settings)
