import { BellRing, ShieldCheck } from "lucide-react";
import { dateTimeMs, money, pct, valueTone } from "../lib/format";
import type { AlphaCarryOpportunity, RealtimeSnapshot } from "../types/api";

type Props = {
  opportunities: AlphaCarryOpportunity[];
  candidates: AlphaCarryOpportunity[];
};

export function AlphaAlert({ opportunities, candidates }: Props) {
  return (
    <div className="page-grid">
      <section className="panel wide">
        <div className="section-title">
          <BellRing size={18} />
          <h2>币安 Alpha 正向套利机会提醒</h2>
        </div>
        <AlphaAlertNote />
        <AlphaTable rows={opportunities} mode="ready" />
        {opportunities.length === 0 && <div className="empty">暂无同时满足合约溢价、正资金费率和成交量阈值的 Alpha 提醒机会。</div>}
      </section>

      <section className="panel wide">
        <div className="section-title">
          <ShieldCheck size={18} />
          <h2>Alpha 候选与未满足原因</h2>
        </div>
        <AlphaTable rows={candidates} mode="blocked" />
        {candidates.length === 0 && <div className="empty">Alpha 提醒后台加载中或暂无候选。</div>}
      </section>
    </div>
  );
}

export function AlphaAlertDashboard({ snapshot }: { snapshot: RealtimeSnapshot }) {
  return (
    <div className="page-grid">
      <section className="panel wide">
        <div className="section-title">
          <BellRing size={18} />
          <h2>币安 Alpha 正向套利机会提醒</h2>
        </div>
        <AlphaAlertNote />
        <div className="metric-grid">
          <div className="metric">
            <span>满足条件</span>
            <strong>{snapshot.alpha_alert_opportunities.length}</strong>
          </div>
          <div className="metric">
            <span>候选数量</span>
            <strong>{snapshot.alpha_alert_candidates.length}</strong>
          </div>
          <div className="metric">
            <span>提醒本金</span>
            <strong>{money(snapshot.settings.alpha_alert_notional_usdt, 2)} USDT</strong>
          </div>
          <div className="metric">
            <span>最低合约溢价</span>
            <strong>{pct(snapshot.settings.alpha_alert_min_basis_pct)}</strong>
          </div>
        </div>
      </section>

      <section className="panel wide">
        <div className="section-title">
          <BellRing size={18} />
          <h2>当前满足条件</h2>
        </div>
        <AlphaTable rows={snapshot.alpha_alert_opportunities} mode="ready" />
        {snapshot.alpha_alert_opportunities.length === 0 && <div className="empty">暂无满足参数的 Alpha 提醒机会。</div>}
      </section>
    </div>
  );
}

function AlphaAlertNote() {
  return (
    <div className="notice-line">
      只做提醒，不会自动买入 Alpha，也不会自动开合约。Alpha 价格来自币安公开 Alpha 行情，需人工确认可交易性和同币种身份。
    </div>
  );
}

function AlphaTable({ rows, mode }: { rows: AlphaCarryOpportunity[]; mode: "ready" | "blocked" }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>排名</th>
            <th>币种</th>
            <th>Alpha 标识</th>
            <th>链</th>
            <th>Alpha 参考价</th>
            <th>合约买一价</th>
            <th>合约卖一价</th>
            <th>合约溢价</th>
            <th>资金费率</th>
            <th>提醒本金</th>
            <th>Alpha 24h 量</th>
            <th>合约 24h 量</th>
            <th>价差收益</th>
            <th>资金费收入</th>
            <th>手续费预留</th>
            <th>净利预估</th>
            <th>{mode === "ready" ? "状态" : "未满足原因"}</th>
            <th>更新时间</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((item, index) => (
            <tr key={`${item.alpha_trade_symbol}-${item.perp_symbol}`}>
              <td>{index + 1}</td>
              <td className="strong">{item.symbol}</td>
              <td>{item.alpha_trade_symbol}</td>
              <td>{item.chain_name || "-"}</td>
              <td>{money(item.alpha_price, 8)}</td>
              <td>{money(item.perp_bid_price, 8)}</td>
              <td>{money(item.perp_ask_price, 8)}</td>
              <td className={valueTone(item.basis_pct)}>{pct(item.basis_pct)}</td>
              <td className={valueTone(item.funding_rate_pct)}>{pct(item.funding_rate_pct)}</td>
              <td>{money(item.notional_usdt, 2)}</td>
              <td>{money(item.alpha_volume_24h_usdt, 0)}</td>
              <td>{money(item.perp_volume_24h_usdt, 0)}</td>
              <td className={valueTone(item.estimated_basis_profit)}>{money(item.estimated_basis_profit, 4)}</td>
              <td className={valueTone(item.estimated_funding_income)}>{money(item.estimated_funding_income, 4)}</td>
              <td>{money(item.estimated_fee_reserve, 4)}</td>
              <td className={valueTone(item.estimated_net_profit)}>{money(item.estimated_net_profit, 4)}</td>
              <td>{item.blocked_reasons.length > 0 ? item.blocked_reasons.join(" / ") : "满足提醒条件，只提示不下单"}</td>
              <td>{dateTimeMs(item.updated_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
