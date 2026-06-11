import { useEffect, useState } from "react";
import { Activity, History, KeyRound, ListFilter, Settings } from "lucide-react";
import { ApiCredentialsPage } from "./components/ApiCredentialsPage";
import { AiPanel } from "./components/AiPanel";
import { CashCarry, CashCarryDashboard } from "./components/CashCarry";
import { HomePage } from "./components/HomePage";
import { Mt4Spread, Mt4SpreadDashboard } from "./components/Mt4Spread";
import { RiskPanel } from "./components/RiskPanel";
import { SettingsPage } from "./components/SettingsPage";
import { Trades } from "./components/Trades";
import { createRealtimeSocket, fetchSnapshot } from "./lib/api";
import type { AIInsight, RealtimeSnapshot, RiskEvent, TradeHistory } from "./types/api";

type Tab = "dashboard" | "opportunities" | "trades";
type Module = "home" | "cash-carry" | "mt4-spread" | "settings" | "api-credentials";

const tabs: Array<{ id: Tab; label: string; icon: typeof Activity }> = [
  { id: "dashboard", label: "仪表盘", icon: Activity },
  { id: "opportunities", label: "机会排行", icon: ListFilter },
  { id: "trades", label: "做单历史", icon: History },
];

export function App() {
  const [activeModule, setActiveModule] = useState<Module>("home");
  const [activeTab, setActiveTab] = useState<Tab>("dashboard");
  const [snapshot, setSnapshot] = useState<RealtimeSnapshot | null>(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    fetchSnapshot().then((next) => setSnapshot((current) => stabilizeSnapshot(current, next))).catch((reason) => setError(String(reason)));
    const socket = createRealtimeSocket(
      (next) => {
        setSnapshot((current) => stabilizeSnapshot(current, next));
        setConnected(true);
        setError("");
      },
      () => {
        setConnected(false);
        setError("实时连接异常");
      },
    );
    socket.onclose = () => setConnected(false);
    return () => socket.close();
  }, []);

  if (!snapshot) {
    return (
      <main className="app loading">
        <div>正在连接后端数据...</div>
        {error && <small>{error}</small>}
      </main>
    );
  }

  return (
    <main className="app">
      <header className="topbar">
        <div>
          <h1>{titleFor(activeModule)}</h1>
          <p>
            数据源：{snapshot.data_source} · WebSocket：{connected ? "已连接" : "未连接"} · 更新时间：
            {new Date(snapshot.generated_at).toLocaleTimeString("zh-CN")}
          </p>
        </div>
        <nav className="tabs">
          <button className={activeModule === "home" ? "active" : ""} onClick={() => setActiveModule("home")} title="首页">
            <Activity size={17} />
            <span>首页</span>
          </button>
          <button className={activeModule === "settings" ? "active" : ""} onClick={() => setActiveModule("settings")} title="参数设置">
            <Settings size={17} />
            <span>参数设置</span>
          </button>
          <button className={activeModule === "api-credentials" ? "active" : ""} onClick={() => setActiveModule("api-credentials")} title="API 管理">
            <KeyRound size={17} />
            <span>API 管理</span>
          </button>
          {(activeModule === "cash-carry" || activeModule === "mt4-spread") && tabs.map((tab) => {
            const Icon = tab.icon;
            return (
              <button key={tab.id} className={activeTab === tab.id ? "active" : ""} onClick={() => setActiveTab(tab.id)} title={tab.label}>
                <Icon size={17} />
                <span>{tab.label}</span>
              </button>
            );
          })}
        </nav>
      </header>

      <div className="content-shell">
        <div className="main-panel">
          {activeModule === "home" && (
            <HomePage
              snapshot={snapshot}
              onOpen={(module) => {
                setActiveModule(module);
                setActiveTab("dashboard");
              }}
            />
          )}
          {activeModule === "settings" && (
            <SettingsPage
              settings={snapshot.settings}
              onSaved={(settings) => setSnapshot({ ...snapshot, settings })}
            />
          )}
          {activeModule === "api-credentials" && <ApiCredentialsPage />}
          {activeModule === "cash-carry" && activeTab === "dashboard" && <CashCarryDashboard snapshot={snapshot} />}
          {activeModule === "cash-carry" && activeTab === "opportunities" && (
            <CashCarry
              opportunities={snapshot.cash_carry_opportunities ?? []}
              candidates={snapshot.cash_carry_candidates ?? []}
            />
          )}
          {activeModule === "cash-carry" && activeTab === "trades" && (
            <Trades
              trades={filterTrades(snapshot.trades, "cash_carry")}
              strategy="cash_carry"
              title="各所期现正向套利做单历史"
              emptyText="各所期现正向套利还没有经过交易所成交回执核验的真实历史单。"
            />
          )}
          {activeModule === "mt4-spread" && activeTab === "dashboard" && <Mt4SpreadDashboard snapshot={snapshot} />}
          {activeModule === "mt4-spread" && activeTab === "opportunities" && (
            <Mt4Spread
              opportunities={snapshot.mt4_spread_opportunities ?? []}
              candidates={snapshot.mt4_spread_candidates ?? []}
            />
          )}
          {activeModule === "mt4-spread" && activeTab === "trades" && (
            <Trades
              trades={filterTrades(snapshot.trades, "mt4_spread")}
              strategy="mt4_spread"
              title="MT4 与五所合约价差做单历史"
              emptyText="MT4 与五所合约价差套利还没有经过交易所成交回执核验的真实历史单。"
            />
          )}
        </div>
        <div className="side-column">
          <AiPanel insight={snapshot.ai_insight} />
          <RiskPanel events={snapshot.risk_events} credentials={snapshot.credential_status} />
        </div>
      </div>
    </main>
  );
}

function titleFor(module: Module): string {
  if (module === "cash-carry") return "各所期现正向套利";
  if (module === "mt4-spread") return "MT4 与五所合约价差套利";
  if (module === "settings") return "全局参数设置";
  if (module === "api-credentials") return "API 管理";
  return "套利策略首页";
}

function filterTrades(trades: TradeHistory[], strategy: TradeHistory["strategy_type"]): TradeHistory[] {
  return trades.filter((trade) => trade.strategy_type === strategy);
}

function stabilizeSnapshot(current: RealtimeSnapshot | null, next: RealtimeSnapshot): RealtimeSnapshot {
  if (!current) return next;
  return {
    ...next,
    risk_events: stabilizeRiskEvents(current.risk_events, next.risk_events),
    ai_insight: stabilizeAiInsight(current.ai_insight, next.ai_insight),
  };
}

function stabilizeRiskEvents(current: RiskEvent[], next: RiskEvent[]): RiskEvent[] {
  const previous = new Map(current.map((event) => [event.id, event]));
  return next.map((event) => {
    const old = previous.get(event.id);
    if (!old || riskEventChanged(old, event)) return event;
    return { ...event, created_at: old.created_at };
  });
}

function riskEventChanged(left: RiskEvent, right: RiskEvent): boolean {
  return left.severity !== right.severity || left.title !== right.title || left.detail !== right.detail || left.action !== right.action;
}

function stabilizeAiInsight(current: AIInsight, next: AIInsight): AIInsight {
  if (
    current.provider === next.provider &&
    current.model === next.model &&
    current.status === next.status &&
    current.content === next.content &&
    current.next_refresh_at === next.next_refresh_at
  ) {
    return { ...next, updated_at: current.updated_at };
  }
  return next;
}
