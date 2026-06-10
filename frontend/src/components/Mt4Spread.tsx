import { Activity, BarChart3 } from "lucide-react";
import { dateTimeMs, money, pct, valueTone } from "../lib/format";
import type { Mt4SpreadOpportunity, RealtimeSnapshot } from "../types/api";

type Props = {
  opportunities: Mt4SpreadOpportunity[];
  candidates: Mt4SpreadOpportunity[];
};

export function Mt4Spread({ opportunities, candidates }: Props) {
  return (
    <div className="page-grid">
      <section className="panel wide">
        <div className="section-title">
          <BarChart3 size={18} />
          <h2>MT4 与五所合约可开仓机会</h2>
        </div>
        <Mt4SpreadTable rows={opportunities} mode="ready" />
        {opportunities.length === 0 && <div className="empty">暂无满足 MT4 与五所合约价差、资金费率和隔夜费条件的机会。</div>}
      </section>

      <section className="panel wide">
        <div className="section-title">
          <BarChart3 size={18} />
          <h2>MT4 候选与不能开仓原因</h2>
        </div>
        <Mt4SpreadTable rows={candidates} mode="blocked" />
        {candidates.length === 0 && <div className="empty">等待 MT4 插件报价推送或暂无候选。</div>}
      </section>
    </div>
  );
}

export function Mt4SpreadDashboard({ snapshot }: { snapshot: RealtimeSnapshot }) {
  const opportunities = snapshot.mt4_spread_opportunities ?? [];
  const candidates = snapshot.mt4_spread_candidates ?? [];
  return (
    <div className="page-grid">
      <section className="panel">
        <div className="section-title">
          <Activity size={18} />
          <h2>MT4 价差运行状态</h2>
        </div>
        <div className="metric-grid">
          <div className="metric">
            <span>可开仓</span>
            <strong>{opportunities.length}</strong>
          </div>
          <div className="metric">
            <span>候选</span>
            <strong>{candidates.length}</strong>
          </div>
          <div className="metric">
            <span>名义本金</span>
            <strong>{money(snapshot.settings.mt4_notional_usdt, 2)}</strong>
          </div>
          <div className="metric">
            <span>杠杆</span>
            <strong>{money(snapshot.settings.mt4_default_leverage, 1)}x</strong>
          </div>
        </div>
      </section>

      <section className="panel wide">
        <div className="section-title">
          <BarChart3 size={18} />
          <h2>MT4 价差监控</h2>
        </div>
        <Mt4SpreadTable rows={opportunities.length > 0 ? opportunities : candidates.slice(0, 20)} mode={opportunities.length > 0 ? "ready" : "blocked"} />
      </section>
    </div>
  );
}

function Mt4SpreadTable({ rows, mode }: { rows: Mt4SpreadOpportunity[]; mode: "ready" | "blocked" }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>排名</th>
            <th>品种</th>
            <th>类型</th>
            <th>交易所</th>
            <th>交易所合约</th>
            <th>做多端</th>
            <th>做空端</th>
            <th>MT4 Bid/Ask</th>
            <th>交易所 Bid/Ask</th>
            <th>价差</th>
            <th>名义本金</th>
            <th>预估保证金</th>
            <th>杠杆</th>
            <th>资金费率收支</th>
            <th>MT4隔夜费收支</th>
            <th>开平手续费</th>
            <th>净利预估</th>
            <th>{mode === "ready" ? "状态" : "不能开仓原因"}</th>
            <th>更新时间</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((item, index) => (
            <tr key={`${item.instrument}-${item.exchange}-${item.exchange_symbol}`}>
              <td>{index + 1}</td>
              <td className="strong">{item.instrument}</td>
              <td>{item.instrument_type === "stock" ? "美股个股" : "大宗商品"}</td>
              <td>{item.exchange}</td>
              <td>{item.exchange_symbol}</td>
              <td>{item.long_venue}</td>
              <td>{item.short_venue}</td>
              <td>{money(item.mt4_bid, 4)} / {money(item.mt4_ask, 4)}</td>
              <td>{money(item.exchange_bid, 4)} / {money(item.exchange_ask, 4)}</td>
              <td className={valueTone(item.spread_pct)}>{pct(item.spread_pct)}</td>
              <td>{money(item.notional_usdt, 2)}</td>
              <td>{money(item.margin_required_usdt, 2)}</td>
              <td>{money(item.leverage, 1)}x</td>
              <td className={valueTone(item.estimated_exchange_funding_net)}>{money(item.estimated_exchange_funding_net, 4)}</td>
              <td className={valueTone(item.estimated_mt4_overnight_net)}>{money(item.estimated_mt4_overnight_net, 4)}</td>
              <td>{money(item.estimated_open_close_fee, 4)}</td>
              <td className={valueTone(item.estimated_net_profit)}>{money(item.estimated_net_profit, 4)}</td>
              <td>{item.blocked_reasons.length > 0 ? item.blocked_reasons.join(" / ") : "满足条件"}</td>
              <td>{dateTimeMs(item.updated_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
