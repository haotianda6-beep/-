import hashlib
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx

from app.core.credentials import CredentialStore
from app.core.env import ai_status
from app.core.models import (
    AIInsight,
    CashCarryOpportunity,
    CashCarryPositionRow,
    ExchangeBalance,
    Opportunity,
    OpportunityCandidate,
    PositionSnapshot,
    RiskEvent,
)


class DeepSeekMonitor:
    def __init__(self) -> None:
        self._cache: AIInsight | None = None
        self._cache_at: datetime | None = None
        self._cache_signature = ""
        self._lock = threading.Lock()
        self._refreshing = False
        self._refresh_started_at: datetime | None = None

    def insight(
        self,
        balances: list[ExchangeBalance],
        positions: list[PositionSnapshot],
        opportunities: list[Opportunity],
        risk_events: list[RiskEvent],
        enabled: bool,
        opportunity_candidates: list[OpportunityCandidate] | None = None,
        cash_carry_positions: list[CashCarryPositionRow] | None = None,
        cash_carry_opportunities: list[CashCarryOpportunity] | None = None,
        cash_carry_candidates: list[CashCarryOpportunity] | None = None,
        reverse_cash_carry_opportunities: list[CashCarryOpportunity] | None = None,
        reverse_cash_carry_candidates: list[CashCarryOpportunity] | None = None,
        strategy_switches: dict[str, Any] | None = None,
    ) -> AIInsight:
        now = datetime.now(timezone.utc)
        status = ai_status()
        provider = str(status.get("provider") or "")
        model = str(status.get("model") or "")
        if not enabled:
            return self._static("disabled", provider, model, "AI 风险监控已在参数设置中关闭。", now)
        if provider != "deepseek" or not status.get("configured"):
            return self._static("not_configured", provider or "deepseek", model or "deepseek-chat", "DeepSeek API key 未配置，暂时只显示规则风控事件。", now)
        extra = {
            "opportunity_candidates": opportunity_candidates or [],
            "cash_carry_positions": cash_carry_positions or [],
            "cash_carry_opportunities": cash_carry_opportunities or [],
            "cash_carry_candidates": cash_carry_candidates or [],
            "reverse_cash_carry_opportunities": reverse_cash_carry_opportunities or [],
            "reverse_cash_carry_candidates": reverse_cash_carry_candidates or [],
            "strategy_switches": strategy_switches or {},
        }
        signature = self._signature(balances, positions, opportunities, risk_events, extra)
        with self._lock:
            refresh_stuck = self._refreshing and self._refresh_started_at and (now - self._refresh_started_at).total_seconds() > 45
            if refresh_stuck:
                self._refreshing = False
            cache_matches = signature == self._cache_signature
            cache_fresh = self._cache and self._cache_at and cache_matches and (now - self._cache_at).total_seconds() < 180
            if cache_fresh:
                return self._cache
            fallback = self._cache if cache_matches and self._cache else self._static("ready", provider, model, "DeepSeek 风控分析后台生成中。", now)
            if not self._refreshing:
                self._refreshing = True
                self._refresh_started_at = now
                threading.Thread(
                    target=self._refresh,
                    args=(list(balances), list(positions), list(opportunities), list(risk_events), extra, signature),
                    daemon=True,
                    name="deepseek-monitor-refresh",
                ).start()
            return fallback

    def invalidate(self) -> None:
        with self._lock:
            self._cache = None
            self._cache_at = None
            self._cache_signature = ""
            self._refreshing = False
            self._refresh_started_at = None

    def _refresh(
        self,
        balances: list[ExchangeBalance],
        positions: list[PositionSnapshot],
        opportunities: list[Opportunity],
        risk_events: list[RiskEvent],
        extra: dict[str, list],
        signature: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        try:
            insight = self._call_deepseek(balances, positions, opportunities, risk_events, extra, now)
        except Exception as exc:  # noqa: BLE001
            insight = AIInsight(provider="deepseek", model="deepseek-chat", status="error", content=f"DeepSeek 后台刷新异常：{str(exc)[:240]}", updated_at=now, next_refresh_at=now + timedelta(seconds=180))
        with self._lock:
            self._cache = insight
            self._cache_at = now
            self._cache_signature = signature
            self._refreshing = False
            self._refresh_started_at = None

    def _call_deepseek(
        self,
        balances: list[ExchangeBalance],
        positions: list[PositionSnapshot],
        opportunities: list[Opportunity],
        risk_events: list[RiskEvent],
        extra: dict[str, list],
        now: datetime,
    ) -> AIInsight:
        deepseek = CredentialStore().deepseek_credentials()
        base_url = deepseek["base_url"].rstrip("/")
        model = deepseek["model"]
        api_key = deepseek["api_key"]
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是永续合约价差套利系统的风控监控。只输出风险观察、异常、建议；不要直接要求自动下单。"},
                {"role": "user", "content": self._build_prompt(balances, positions, opportunities, risk_events, extra)},
            ],
            "temperature": 0.2,
            "max_tokens": 700,
            "stream": False,
        }
        try:
            with httpx.Client(timeout=20, trust_env=False) as client:
                response = client.post(
                    f"{base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
            content = data["choices"][0]["message"]["content"].strip()
            return AIInsight(provider="deepseek", model=model, status="ready", content=content, updated_at=now, next_refresh_at=now + timedelta(seconds=180))
        except Exception as exc:  # noqa: BLE001
            detail = str(exc)
            if api_key:
                detail = detail.replace(api_key, "***")
            return AIInsight(provider="deepseek", model=model, status="error", content=f"DeepSeek 调用失败：{detail[:240]}", updated_at=now, next_refresh_at=now + timedelta(seconds=180))

    def _build_prompt(
        self,
        balances: list[ExchangeBalance],
        positions: list[PositionSnapshot],
        opportunities: list[Opportunity],
        risk_events: list[RiskEvent],
        extra: dict[str, list],
    ) -> str:
        data: dict[str, Any] = {
            "balances": [{"exchange": item.exchange, "equity": str(item.equity_usdt), "available": str(item.available_usdt)} for item in balances],
            "positions": [{"exchange": item.exchange, "symbol": item.symbol, "side": item.side, "qty": str(item.quantity), "pnl": str(item.unrealized_pnl)} for item in positions],
            "top_cross_opportunities": [
                {
                    "symbol": item.symbol,
                    "long_exchange": item.long_exchange,
                    "short_exchange": item.short_exchange,
                    "spread_pct": str(item.spread_pct),
                    "funding_net": str(item.estimated_funding_net),
                    "net_profit": str(item.estimated_net_profit),
                    "risk_tags": item.risk_tags,
                }
                for item in opportunities[:5]
            ],
            "cross_candidates": [
                {
                    "symbol": item.symbol,
                    "long_exchange": item.long_exchange,
                    "short_exchange": item.short_exchange,
                    "spread_pct": str(item.spread_pct),
                    "net_profit": str(item.estimated_net_profit),
                    "blocked_reasons": item.blocked_reasons[:3],
                }
                for item in extra["opportunity_candidates"][:5]
            ],
            "cash_carry_positions": [
                {
                    "exchange": item.exchange,
                    "symbol": item.symbol,
                    "status": item.status,
                    "spot_qty": str(item.spot_quantity),
                    "perp_base_qty": str(item.perp_base_quantity),
                    "quantity_gap": str(item.quantity_gap),
                    "basis_pct": str(item.basis_pct),
                    "funding_income_est": str(item.estimated_funding_income),
                    "current_net_profit": str(item.current_net_profit),
                }
                for item in extra["cash_carry_positions"][:5]
            ],
            "strategy_switches": extra["strategy_switches"],
            "cash_carry": self._cash_rows(extra["cash_carry_opportunities"], extra["cash_carry_candidates"]),
            "reverse_cash_carry": self._cash_rows(extra["reverse_cash_carry_opportunities"], extra["reverse_cash_carry_candidates"]),
            "risk_events": [{"severity": item.severity, "title": item.title, "detail": item.detail} for item in risk_events[:8]],
        }
        return f"请用中文输出 3-6 条简短风控观察，按重要性排序。有持仓时优先只分析当前持仓的止盈、收敛、资金费率、数量匹配和退出风险，不要把其他候选机会当主线。正向期现开仓基差只看 strategy_switches.params.cash_carry_min_basis_pct，平仓/收敛阈值只看 cash_carry_close_basis_pct，固定U止盈只看 take_profit_usdt，不得把开仓阈值当平仓线。以 cash_carry_positions 的当前 status 和数量为准；status=matched 代表系统容忍范围内数量已对齐，禁止把 quantity_gap 单独写成数量风险；只有 status=mismatch、spot_only 或 perp_only 时才提示数量不一致。若 cash_carry.ready 的 exchange+symbol 已存在于 cash_carry_positions，只能表述为已有持仓对应机会仍满足，不能说未开仓或未显示持仓。不要把实盘总开关开启理解为所有策略子开关开启；自动开仓/下单状态必须逐项参考 strategy_switches.enabled 和 strategy_switches.disabled，除非 disabled 为空，否则禁止说子开关均开启。top_cross_opportunities 为空但 candidates 不为空时，应表述为暂无可执行机会但有候选监控，不要说系统无数据。当前系统摘要：{data}"

    def _cash_rows(self, ready: list[CashCarryOpportunity], candidates: list[CashCarryOpportunity]) -> dict[str, Any]:
        def row(item: CashCarryOpportunity) -> dict[str, Any]:
            return {
                "exchange": item.exchange,
                "symbol": item.symbol,
                "basis_pct": str(item.basis_pct),
                "funding_rate_pct": str(item.funding_rate_pct),
                "net_profit": str(item.estimated_net_profit),
                "blocked_reasons": item.blocked_reasons[:3],
            }
        return {"ready": [row(item) for item in ready[:5]], "candidates": [row(item) for item in candidates[:5]]}

    def _static(self, status: str, provider: str, model: str, content: str, now: datetime) -> AIInsight:
        return AIInsight(provider=provider, model=model, status=status, content=content, updated_at=now)

    def _signature(
        self,
        balances: list[ExchangeBalance],
        positions: list[PositionSnapshot],
        opportunities: list[Opportunity],
        risk_events: list[RiskEvent],
        extra: dict[str, list] | None = None,
    ) -> str:
        extra = extra or {}
        text = "|".join([
            ",".join(sorted(str(item.exchange) for item in balances)),
            ",".join(sorted(f"{item.exchange}:{item.symbol}:{item.side}" for item in positions)),
            ",".join(f"{item.symbol}:{item.long_exchange}:{item.short_exchange}:{self._bucket(item.spread_pct)}" for item in opportunities[:5]),
            ",".join(f"{item.severity}:{item.title}:{item.detail}" for item in risk_events),
            ",".join(f"{item.exchange}:{item.symbol}:{item.status}:{item.add_count}:{self._bucket(item.basis_pct)}" for item in extra.get("cash_carry_positions", [])),
            ",".join(f"{item.exchange}:{item.symbol}:{self._bucket(item.basis_pct)}:{self._reason_key(item.blocked_reasons)}" for item in extra.get("cash_carry_candidates", [])[:5]),
            ",".join(f"{item.exchange}:{item.symbol}:{item.borrow_check_status}:{self._bucket(item.basis_pct)}:{self._reason_key(item.blocked_reasons)}" for item in extra.get("reverse_cash_carry_candidates", [])[:5]),
            str(sorted((extra.get("strategy_switches") or {}).items())),
        ])
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _bucket(self, value: Any, unit: str = "1") -> str:
        try:
            amount = Decimal(str(value))
            step = Decimal(unit)
            return str(int(amount / step) * step)
        except Exception:
            return str(value)

    def _reason_key(self, reasons: list[str]) -> str:
        return "/".join(reason.split("，", 1)[0].split(" ", 1)[0] for reason in reasons[:3])
