import { ArrowUpRight, BellRing, Landmark, LayoutDashboard } from "lucide-react";
import { money, valueTone } from "../lib/format";
import type { RealtimeSnapshot } from "../types/api";

type ModuleId = "cash-carry" | "alpha-alert" | "mt4-spread";

type Props = {
  snapshot: RealtimeSnapshot;
  onOpen: (module: ModuleId) => void;
};

export function HomePage({ snapshot, onOpen }: Props) {
  const cashProfit = sumProfit(snapshot.cash_carry_opportunities);
  const alphaProfit = sumProfit(snapshot.alpha_alert_opportunities);
  const mt4Profit = sumProfit(snapshot.mt4_spread_opportunities);
  const cashRunning = snapshot.settings.cash_carry_enabled;
  const alphaRunning = snapshot.settings.alpha_alert_enabled;
  const mt4Running = snapshot.settings.mt4_spread_enabled;
  return (
    <section className="module-grid">
      <button className="module-card" onClick={() => onOpen("cash-carry")}>
        <div className="module-head">
          <Landmark size={18} />
          <span>{cashRunning ? (snapshot.settings.cash_carry_auto_open_enabled ? "自动运行" : "监控中") : "已关闭"}</span>
        </div>
        <h2>GATE / BITGET 期现正向套利</h2>
        <div className="module-metrics">
          <span>可开仓 <strong>{snapshot.cash_carry_opportunities.length}</strong></span>
          <span>候选 <strong>{snapshot.cash_carry_candidates.length}</strong></span>
        </div>
        <div className={`module-profit ${valueTone(cashProfit)}`}>预估盈利 {money(cashProfit, 4)} USDT</div>
      </button>

      <button className="module-card" onClick={() => onOpen("alpha-alert")}>
        <div className="module-head">
          <BellRing size={18} />
          <span>{alphaRunning ? "只提醒" : "已关闭"}</span>
        </div>
        <h2>币安 Alpha 正向套利机会提醒</h2>
        <div className="module-metrics">
          <span>满足条件 <strong>{snapshot.alpha_alert_opportunities.length}</strong></span>
          <span>候选 <strong>{snapshot.alpha_alert_candidates.length}</strong></span>
        </div>
        <div className={`module-profit ${valueTone(alphaProfit)}`}>预估净利 {money(alphaProfit, 4)} USDT</div>
      </button>

      <button className="module-card" onClick={() => onOpen("mt4-spread")}>
        <div className="module-head">
          <LayoutDashboard size={18} />
          <span>{mt4Running ? "监控中" : "已关闭"}</span>
        </div>
        <h2>MT4 与五所价差套利</h2>
        <div className="module-metrics">
          <span>可开仓 <strong>{snapshot.mt4_spread_opportunities.length}</strong></span>
          <span>候选 <strong>{snapshot.mt4_spread_candidates.length}</strong></span>
        </div>
        <div className={`module-profit ${valueTone(mt4Profit)}`}>预估盈利 {money(mt4Profit, 4)} USDT</div>
      </button>

      <button className="module-card" onClick={() => window.location.assign("/xau-arb/")}>
        <div className="module-head">
          <ArrowUpRight size={18} />
          <span>独立执行器</span>
        </div>
        <h2>MT4 / Binance 黄金套利</h2>
        <div className="module-metrics">
          <span>品种 <strong>XAUUSD</strong></span>
          <span>入口 <strong>/xau-arb/</strong></span>
        </div>
        <div className="module-profit neutral">打开黄金价差执行页面</div>
      </button>
    </section>
  );
}

function sumProfit(items: Array<{ estimated_net_profit: string }> | undefined): number {
  return (items ?? []).reduce((total, item) => total + (Number(item.estimated_net_profit) || 0), 0);
}
