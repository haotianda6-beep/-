import { Landmark, LayoutDashboard } from "lucide-react";
import { money, valueTone } from "../lib/format";
import type { RealtimeSnapshot } from "../types/api";

type ModuleId = "cash-carry" | "mt4-spread";

type Props = {
  snapshot: RealtimeSnapshot;
  onOpen: (module: ModuleId) => void;
};

export function HomePage({ snapshot, onOpen }: Props) {
  const cashProfit = sumProfit(snapshot.cash_carry_opportunities);
  const mt4Profit = sumProfit(snapshot.mt4_spread_opportunities);
  const cashRunning = snapshot.settings.cash_carry_enabled;
  const mt4Running = snapshot.settings.mt4_spread_enabled;
  return (
    <section className="module-grid">
      <button className="module-card" onClick={() => onOpen("cash-carry")}>
        <div className="module-head">
          <Landmark size={18} />
          <span>{cashRunning ? (snapshot.settings.cash_carry_auto_open_enabled ? "自动运行" : "监控中") : "已关闭"}</span>
        </div>
        <h2>各所期现正向套利</h2>
        <div className="module-metrics">
          <span>可开仓 <strong>{snapshot.cash_carry_opportunities.length}</strong></span>
          <span>候选 <strong>{snapshot.cash_carry_candidates.length}</strong></span>
        </div>
        <div className={`module-profit ${valueTone(cashProfit)}`}>预估盈利 {money(cashProfit, 4)} USDT</div>
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
    </section>
  );
}

function sumProfit(items: Array<{ estimated_net_profit: string }> | undefined): number {
  return (items ?? []).reduce((total, item) => total + (Number(item.estimated_net_profit) || 0), 0);
}
