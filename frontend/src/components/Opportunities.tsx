import { ArrowDownUp } from "lucide-react";
import type { Opportunity, OpportunityCandidate } from "../types/api";
import { dateTimeMs, money, pct, valueTone } from "../lib/format";

type Props = {
  opportunities: Opportunity[];
  candidates: OpportunityCandidate[];
};

export function Opportunities({ opportunities, candidates }: Props) {
  return (
    <div className="page-grid">
      <section className="panel wide">
        <div className="section-title">
          <ArrowDownUp size={18} />
          <h2>可开仓机会</h2>
        </div>
        <OpportunityTable rows={opportunities} mode="ready" />
        {opportunities.length === 0 && <div className="empty">暂无通过价差、资金费率、成交量、链路和深度校验的可开仓机会。</div>}
      </section>

      <section className="panel wide">
        <div className="section-title">
          <ArrowDownUp size={18} />
          <h2>候选机会与不能开仓原因</h2>
        </div>
        <OpportunityTable rows={candidates} mode="blocked" />
        {candidates.length === 0 && <div className="empty">暂无达到基础价差、资金费率、成交量和现货市场初筛的候选机会。</div>}
      </section>
    </div>
  );
}

function OpportunityTable({ rows, mode }: { rows: Array<Opportunity | OpportunityCandidate>; mode: "ready" | "blocked" }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>排名</th>
            <th>合约</th>
            <th>做多所</th>
            <th>做空所</th>
            <th>多所价格</th>
            <th>空所价格</th>
            <th>价差</th>
            <th>名义本金</th>
            <th>预估保证金</th>
            <th>杠杆</th>
            <th>最低 24h 量</th>
            <th>开平手续费</th>
            <th>资金费率预估</th>
            <th>净利预估</th>
            <th>现货互通</th>
            <th>深度</th>
            <th>{mode === "ready" ? "风险标签" : "不能开仓原因"}</th>
            <th>更新时间</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((item, index) => (
            <tr key={`${item.symbol}-${item.long_exchange}-${item.short_exchange}`}>
              <td>{index + 1}</td>
              <td className="strong">{item.symbol}</td>
              <td>{item.long_exchange}</td>
              <td>{item.short_exchange}</td>
              <td>{money(item.long_price, 6)}</td>
              <td>{money(item.short_price, 6)}</td>
              <td className="positive">{pct(item.spread_pct)}</td>
              <td>{money(item.notional_usdt, 2)}</td>
              <td>{money(item.margin_required_usdt, 2)}</td>
              <td>{money(item.leverage, 1)}x</td>
              <td>{money(item.min_volume_24h_usdt, 0)}</td>
              <td>{money(item.estimated_open_close_fee, 4)}</td>
              <td className={valueTone(item.estimated_funding_net)}>{money(item.estimated_funding_net, 4)}</td>
              <td className={valueTone(item.estimated_net_profit)}>{money(item.estimated_net_profit, 4)}</td>
              <td>{item.spot_transfer_ok ? "可互通" : "不通过"}</td>
              <td>{item.depth_ok ? "充足" : "不通过"}</td>
              <td>{mode === "blocked" ? labelBlockedReasons(item) : labelRiskTags(item.risk_tags)}</td>
              <td>{dateTimeMs(item.updated_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function labelBlockedReasons(item: Opportunity | OpportunityCandidate): string {
  if ("blocked_reasons" in item && item.blocked_reasons.length > 0) {
    return item.blocked_reasons.join(" / ");
  }
  return "-";
}

function labelRiskTags(tags: string[]): string {
  if (tags.length === 0) return "-";
  return tags
    .map((tag) => {
      if (tag === "spot-market-only") return "未校验链路";
      if (tag === "orderbook-depth-not-verified") return "未校验盘口深度";
      return tag;
    })
    .join(" / ");
}
