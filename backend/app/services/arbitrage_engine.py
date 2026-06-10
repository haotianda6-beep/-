from datetime import datetime, timezone
from decimal import Decimal
from itertools import permutations

from app.core.models import (
    BotSettings,
    CashCarryPositionRow,
    DashboardRow,
    DataSource,
    ExchangeBalance,
    ExchangeName,
    Opportunity,
    PositionSnapshot,
    RealtimeSnapshot,
    RiskEvent,
    TradeHistory,
)
from app.core.market_math import FEE_RATES, q
from app.core.env import ai_status, credential_statuses, env_bool
from app.core.pnl import (
    calculate_current_net_profit,
    calculate_funding_net,
    calculate_spread_pct,
)
from app.services.ai_monitor import DeepSeekMonitor
from app.services.cash_carry_positions import CashCarryPositionBuilder
from app.services.cash_carry_scanner import CashCarryScanner
from app.services.demo_market_data import BASE_QUOTES, INTEROPERABLE_SYMBOLS, MarketQuote
from app.services.execution_state import recent_execution_results
from app.services.live_opportunities import LiveOpportunityScanner
from app.services.live_read import LiveReadService
from app.services.live_runtime import LiveRuntimeCache
from app.services.mt4_bridge import Mt4QuoteStore, Mt4SpreadScanner
from app.services.reverse_cash_carry_scanner import ReverseCashCarryScanner
from app.services.settings_store import SettingsStore
from app.services.trade_history_store import TradeHistoryStore
from app.services.ws_ticker_cache import WSTickerCache


class ArbitrageEngine:
    def __init__(self, settings_store: SettingsStore) -> None:
        self.settings_store = settings_store
        self.live_read = LiveReadService()
        self.ticker_cache = WSTickerCache()
        self.live_scanner = LiveOpportunityScanner()
        self.cash_carry_scanner = CashCarryScanner()
        self.cash_carry_positions = CashCarryPositionBuilder(self.ticker_cache)
        self.reverse_cash_carry_scanner = ReverseCashCarryScanner()
        self.mt4_quote_store = Mt4QuoteStore()
        self.mt4_spread_scanner = Mt4SpreadScanner(self.mt4_quote_store)
        self.trade_history = TradeHistoryStore()
        self.ai_monitor = DeepSeekMonitor()
        self.live_runtime = LiveRuntimeCache(
            self.live_read,
            self.live_scanner,
            self.cash_carry_scanner,
            self.reverse_cash_carry_scanner,
            self.mt4_spread_scanner,
            ticker_cache=self.ticker_cache,
        )

    def snapshot(self) -> RealtimeSnapshot:
        settings = self.settings_store.load()
        live_enabled = self.live_read.live_data_enabled()
        live_runtime = self.live_runtime.get(settings) if live_enabled else None
        balances = live_runtime.account.balances if live_runtime else self.get_balances()
        positions = live_runtime.account.positions if live_runtime else self.get_positions(settings)
        opportunities = live_runtime.scan.opportunities if live_runtime else self.get_opportunities(settings)
        cash_prices = (live_runtime.cash_carry.opportunities + live_runtime.cash_carry.candidates) if live_runtime else []
        opportunity_candidates = live_runtime.scan.candidates if live_runtime else []
        cash_opps = live_runtime.cash_carry.opportunities if live_runtime else []
        cash_candidates = live_runtime.cash_carry.candidates if live_runtime else []
        reverse_opps = live_runtime.reverse_cash_carry.opportunities if live_runtime else []
        reverse_candidates = live_runtime.reverse_cash_carry.candidates if live_runtime else []
        mt4_opps = live_runtime.mt4_spread_opportunities if live_runtime else []
        mt4_candidates = live_runtime.mt4_spread_candidates if live_runtime else []
        cash_positions = self.cash_carry_positions.build(positions, cash_prices, settings) if live_enabled else []
        risk_events = self.get_risk_events(
            settings,
            live_runtime.account.issues if live_runtime else [],
            live_runtime.scan.issues if live_runtime else [],
            live_runtime.cash_carry.issues if live_runtime else [],
            live_runtime.reverse_cash_carry.issues if live_runtime else [],
            live_runtime.mt4_spread_issues if live_runtime else [],
            cash_positions,
        )
        return RealtimeSnapshot(
            balances=balances,
            positions=positions,
            dashboard=[] if live_enabled else self.get_dashboard(settings),
            opportunities=opportunities,
            opportunity_candidates=opportunity_candidates,
            cash_carry_opportunities=cash_opps,
            cash_carry_candidates=cash_candidates,
            cash_carry_positions=cash_positions,
            reverse_cash_carry_opportunities=reverse_opps,
            reverse_cash_carry_candidates=reverse_candidates,
            mt4_spread_opportunities=mt4_opps,
            mt4_spread_candidates=mt4_candidates,
            trades=self.get_trades(),
            settings=settings,
            risk_events=risk_events,
            credential_status=credential_statuses(),
            ai_insight=self.ai_monitor.insight(
                balances,
                positions,
                opportunities,
                risk_events,
                settings.ai_risk_monitor_enabled,
                opportunity_candidates,
                cash_positions,
                cash_opps,
                cash_candidates,
                reverse_opps,
                reverse_candidates,
                self._strategy_switches(settings),
            ),
            data_source=DataSource.LIVE if live_enabled else DataSource.MOCK,
        )

    def get_balances(self) -> list[ExchangeBalance]:
        now = datetime.now(timezone.utc)
        return [
            ExchangeBalance(exchange=ExchangeName.BINANCE, equity_usdt=Decimal("5200"), available_usdt=Decimal("4300"), margin_used_usdt=Decimal("900"), updated_at=now),
            ExchangeBalance(exchange=ExchangeName.OKX, equity_usdt=Decimal("4800"), available_usdt=Decimal("4050"), margin_used_usdt=Decimal("750"), updated_at=now),
            ExchangeBalance(exchange=ExchangeName.GATE, equity_usdt=Decimal("2600"), available_usdt=Decimal("2450"), margin_used_usdt=Decimal("150"), updated_at=now),
            ExchangeBalance(exchange=ExchangeName.BITGET, equity_usdt=Decimal("3100"), available_usdt=Decimal("3020"), margin_used_usdt=Decimal("80"), updated_at=now),
            ExchangeBalance(exchange=ExchangeName.BYBIT, equity_usdt=Decimal("3900"), available_usdt=Decimal("3300"), margin_used_usdt=Decimal("600"), updated_at=now),
        ]

    def get_positions(self, settings: BotSettings) -> list[PositionSnapshot]:
        row = self._demo_dashboard_row(settings)
        return [
            PositionSnapshot(exchange=row.long_exchange, symbol=row.symbol, side="long", quantity=row.long_quantity, entry_price=Decimal("70000"), mark_price=self._quote(row.long_exchange, row.symbol).bid, leverage=row.leverage, unrealized_pnl=row.long_unrealized_pnl),
            PositionSnapshot(exchange=row.short_exchange, symbol=row.symbol, side="short", quantity=row.short_quantity, entry_price=Decimal("71100"), mark_price=self._quote(row.short_exchange, row.symbol).ask, leverage=row.leverage, unrealized_pnl=row.short_unrealized_pnl),
        ]

    def get_dashboard(self, settings: BotSettings) -> list[DashboardRow]:
        return [self._demo_dashboard_row(settings)]

    def get_opportunities(self, settings: BotSettings) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        symbols = sorted({symbol for quotes in BASE_QUOTES.values() for symbol in quotes})
        for symbol in symbols:
            if symbol in settings.symbol_blacklist:
                continue
            for long_exchange, short_exchange in permutations(ExchangeName, 2):
                if self._is_exchange_blocked(long_exchange, short_exchange, settings):
                    continue
                long_quote = self._maybe_quote(long_exchange, symbol)
                short_quote = self._maybe_quote(short_exchange, symbol)
                if not long_quote or not short_quote:
                    continue
                opportunity = self._build_opportunity(symbol, long_exchange, short_exchange, long_quote, short_quote, settings)
                if opportunity:
                    opportunities.append(opportunity)
        return sorted(opportunities, key=lambda item: item.estimated_net_profit, reverse=True)

    def get_trades(self) -> list[TradeHistory]:
        return self.trade_history.load()

    def get_risk_events(
        self,
        settings: BotSettings,
        live_issues: list[str] | None = None,
        scan_issues: list[str] | None = None,
        cash_carry_issues: list[str] | None = None,
        reverse_cash_carry_issues: list[str] | None = None,
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
        for index, issue in enumerate(scan_issues or []):
            events.append(RiskEvent(id=f"scan-issue-{index}", severity="warning", title="机会扫描接口异常", detail=issue, action="检查交易所公共行情接口、限流或网络状态。", created_at=now))
        for index, issue in enumerate(cash_carry_issues or []):
            events.append(RiskEvent(id=f"cash-carry-issue-{index}", severity="warning", title="期现扫描接口异常", detail=issue, action="检查同所现货、合约行情和资金费率接口。", created_at=now))
        for index, issue in enumerate(reverse_cash_carry_issues or []):
            events.append(RiskEvent(id=f"reverse-cash-carry-issue-{index}", severity="warning", title="反向期现扫描接口异常", detail=issue, action="检查同所现货、合约行情、资金费率和借币接口。", created_at=now))
        for index, issue in enumerate(mt4_spread_issues or []):
            events.append(RiskEvent(id=f"mt4-spread-issue-{index}", severity="warning", title="MT4 价差扫描异常", detail=issue, action="检查 MT4 插件报价推送、品种映射和交易所合约行情接口。", created_at=now))
        for result in recent_execution_results():
            if result["status"] not in {"failed", "blocked_by_safety_gate"}:
                continue
            if self._stale_cash_carry_result(result, cash_carry_positions or []):
                continue
            events.append(RiskEvent(id=f"execution-{result['strategy_id']}", severity="warning", title=f"{result['title']}未完成", detail=result["reason"], action="按失败原因补齐交易所 API 权限、账户资金或关闭对应自动步骤后再重试。", created_at=now))
        if settings.reverse_cash_carry_enabled:
            events.append(RiskEvent(id="reverse-borrow-check-enabled", severity="info", title="反向期现借币校验已启用", detail="候选会读取可借数量、借币利率并扣减预估借币成本；接口权限或交易所未返回时会阻断开仓。", action="以候选行的借币字段和不能开仓原因为准，实盘前仍需小额人工核验。", created_at=now))
        ai = ai_status()
        if ai["provider"] == "deepseek" and not ai["configured"]:
            events.append(RiskEvent(id="deepseek-missing-key", severity="info", title="DeepSeek 未配置", detail="DeepSeek API key 还没有配置。", action="在 API 管理页面保存 DeepSeek API key 后即可接入 AI 风险监控。", created_at=now))
        if settings.emergency_close_enabled:
            events.append(RiskEvent(id="emergency-close", severity="critical", title="紧急平仓开关已打开", detail="系统应停止新开仓并准备执行保护性平仓。", action="检查持仓并人工确认。", created_at=now))
        return events

    def _stale_cash_carry_result(self, result: dict[str, str], rows: list[CashCarryPositionRow]) -> bool:
        if result.get("strategy_id") != "cash-carry":
            return False
        reason = result.get("reason", "")
        for row in rows:
            exchange = row.exchange.value if hasattr(row.exchange, "value") else str(row.exchange)
            if row.status == "matched" and exchange in reason and row.symbol in reason:
                return True
        return False

    def _strategy_switches(self, settings: BotSettings) -> dict[str, object]:
        switches = {"cross_auto_open": settings.auto_open_enabled, "cross_auto_close": settings.auto_close_enabled, "cash_carry_auto_open": settings.cash_carry_auto_open_enabled, "cash_carry_auto_trade": settings.cash_carry_auto_trade_enabled, "cash_carry_auto_close": settings.cash_carry_auto_close_enabled, "reverse_cash_carry_auto_open": settings.reverse_cash_carry_auto_open_enabled, "reverse_cash_carry_auto_close": settings.reverse_cash_carry_auto_close_enabled, "mt4_spread_enabled": settings.mt4_spread_enabled, "manual_confirm_required": settings.manual_confirm_required}
        return {"enabled": [key for key, value in switches.items() if value], "disabled": [key for key, value in switches.items() if not value], "params": {"cash_carry_min_basis_pct": str(settings.cash_carry_min_basis_pct), "cash_carry_close_basis_pct": str(settings.cash_carry_close_basis_pct), "reverse_cash_carry_min_discount_pct": str(settings.reverse_cash_carry_min_discount_pct), "reverse_cash_carry_close_discount_pct": str(settings.reverse_cash_carry_close_discount_pct), "take_profit_usdt": str(settings.take_profit_usdt), "stop_loss_usdt": str(settings.stop_loss_usdt)}}

    def update_settings(self, settings: BotSettings) -> BotSettings:
        return self.settings_store.save(settings)

    def _build_opportunity(
        self,
        symbol: str,
        long_exchange: ExchangeName,
        short_exchange: ExchangeName,
        long_quote: MarketQuote,
        short_quote: MarketQuote,
        settings: BotSettings,
    ) -> Opportunity | None:
        spread_pct = calculate_spread_pct(long_quote.ask, short_quote.bid)
        min_volume = min(long_quote.volume_24h_usdt, short_quote.volume_24h_usdt)
        funding_net = calculate_funding_net(settings.order_notional_usdt, long_quote.funding_rate, short_quote.funding_rate)
        fee = settings.order_notional_usdt * (FEE_RATES[long_exchange] + FEE_RATES[short_exchange]) * Decimal("2")
        gross = settings.order_notional_usdt * spread_pct / Decimal("100")
        estimated_net = gross - fee + funding_net
        spot_transfer_ok = symbol in INTEROPERABLE_SYMBOLS
        depth_ok = min(long_quote.depth_usdt, short_quote.depth_usdt) >= settings.order_notional_usdt * Decimal("3")
        if spread_pct < settings.min_open_spread_pct:
            return None
        if funding_net < settings.min_funding_net_usdt:
            return None
        if min_volume < settings.min_24h_volume_usdt:
            return None
        if not spot_transfer_ok or not depth_ok:
            return None
        return Opportunity(
            symbol=symbol,
            long_exchange=long_exchange,
            short_exchange=short_exchange,
            long_price=q(long_quote.ask),
            short_price=q(short_quote.bid),
            spread_pct=q(spread_pct),
            long_volume_24h_usdt=long_quote.volume_24h_usdt,
            short_volume_24h_usdt=short_quote.volume_24h_usdt,
            min_volume_24h_usdt=min_volume,
            estimated_open_close_fee=q(fee),
            estimated_funding_net=q(funding_net),
            estimated_net_profit=q(estimated_net),
            notional_usdt=q(settings.order_notional_usdt, "0.01"),
            margin_required_usdt=q(settings.order_notional_usdt / settings.default_leverage if settings.default_leverage > 0 else settings.order_notional_usdt, "0.01"),
            leverage=settings.default_leverage,
            spot_transfer_ok=spot_transfer_ok,
            depth_ok=depth_ok,
            risk_tags=[],
            updated_at=datetime.now(timezone.utc),
        )

    def _demo_dashboard_row(self, settings: BotSettings) -> DashboardRow:
        long_quote = self._quote(ExchangeName.BINANCE, "BTCUSDT")
        short_quote = self._quote(ExchangeName.OKX, "BTCUSDT")
        quantity = q(settings.order_notional_usdt / Decimal("70000"), "0.000001")
        long_pnl = q((long_quote.bid - Decimal("70000")) * quantity)
        short_pnl = q((Decimal("71100") - short_quote.ask) * quantity)
        fee = q(settings.order_notional_usdt * Decimal("0.0018"))
        close_fee = q(settings.order_notional_usdt * Decimal("0.0009"))
        funding = Decimal("0.18")
        net = calculate_current_net_profit(long_pnl, short_pnl, fee, close_fee, funding)
        return DashboardRow(
            trade_pair_id="mock-BTCUSDT-BINANCE-OKX",
            symbol="BTCUSDT",
            long_exchange=ExchangeName.BINANCE,
            short_exchange=ExchangeName.OKX,
            long_quantity=quantity,
            short_quantity=quantity,
            leverage=min(settings.default_leverage, settings.max_leverage),
            long_unrealized_pnl=long_pnl,
            short_unrealized_pnl=short_pnl,
            open_fee=fee,
            estimated_close_fee=close_fee,
            realized_funding_net=funding,
            estimated_funding_net=q(calculate_funding_net(settings.order_notional_usdt, long_quote.funding_rate, short_quote.funding_rate)),
            entry_spread_pct=Decimal("1.5714"),
            current_spread_pct=q(calculate_spread_pct(long_quote.ask, short_quote.bid)),
            add_count=0,
            current_net_profit=q(net),
            updated_at=datetime.now(timezone.utc),
        )

    def _quote(self, exchange: ExchangeName, symbol: str) -> MarketQuote:
        return self._maybe_quote(exchange, symbol) or BASE_QUOTES[exchange][symbol]

    def _maybe_quote(self, exchange: ExchangeName, symbol: str) -> MarketQuote | None:
        quote = BASE_QUOTES.get(exchange, {}).get(symbol)
        if not quote:
            return None
        return quote

    def _is_exchange_blocked(self, long_exchange: ExchangeName, short_exchange: ExchangeName, settings: BotSettings) -> bool:
        blocked = set(settings.exchange_blacklist)
        return long_exchange in blocked or short_exchange in blocked
