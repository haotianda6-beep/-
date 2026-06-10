import { Banknote, Boxes, Layers } from "lucide-react";
import { dateTime, dateTimeMs, money, pct, qty, takeProfitProgress, takeProfitRemaining, valueTone } from "../lib/format";
import type { CashCarryOpportunity, RealtimeSnapshot } from "../types/api";

type Props = {
  opportunities: CashCarryOpportunity[];
  candidates: CashCarryOpportunity[];
};

export function ReverseCashCarry({ opportunities, candidates }: Props) {
  return (
    <div className="page-grid">
      <section className="panel wide">
        <div className="section-title">
          <Boxes size={18} />
          <h2>期现反向套利机会</h2>
        </div>
        <ReverseCashCarryTable rows={opportunities} mode="ready" />
        {opportunities.length === 0 && <div className="empty">暂无通过借币校验的期现反向可开仓机会。</div>}
      </section>

      <section className="panel wide">
        <div className="section-title">
          <Boxes size={18} />
          <h2>期现反向候选与不能开仓原因</h2>
        </div>
        <ReverseCashCarryTable rows={candidates} mode="blocked" />
        {candidates.length === 0 && <div className="empty">反向期现扫描后台加载中或暂无候选。</div>}
      </section>
    </div>
  );
}

export function ReverseCashCarryDashboard({ snapshot }: { snapshot: RealtimeSnapshot }) {
  const ready = snapshot.reverse_cash_carry_opportunities ?? [];
  return (
    <div className="page-grid">
      <section className="panel">
        <div className="section-title">
          <Banknote size={18} />
          <h2>五所余额</h2>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>交易所</th>
                <th>权益 USDT</th>
                <th>可用 USDT</th>
                <th>占用保证金</th>
                <th>更新时间</th>
              </tr>
            </thead>
            <tbody>
              {snapshot.balances.map((item) => (
                <tr key={item.exchange}>
                  <td className="strong">{item.exchange}</td>
                  <td>{money(item.equity_usdt)}</td>
                  <td>{money(item.available_usdt)}</td>
                  <td>{money(item.margin_used_usdt)}</td>
                  <td>{dateTime(item.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel">
        <div className="section-title">
          <Layers size={18} />
          <h2>当前合约持仓</h2>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>币种</th>
                <th>交易所</th>
                <th>方向</th>
                <th>数量</th>
                <th>杠杆</th>
                <th>开仓价</th>
                <th>标记价</th>
                <th>浮盈亏</th>
                <th>止盈目标</th>
                <th>距止盈</th>
                <th>止盈进度</th>
              </tr>
            </thead>
            <tbody>
              {snapshot.positions.map((item) => (
                <tr key={`${item.exchange}-${item.symbol}-${item.side}`}>
                  <td className="strong">{item.symbol}</td>
                  <td>{item.exchange}</td>
                  <td>{item.side === "long" ? "多" : "空"}</td>
                  <td>{qty(item.quantity)}</td>
                  <td>{money(item.leverage, 1)}x</td>
                  <td>{money(item.entry_price, 4)}</td>
                  <td>{money(item.mark_price, 4)}</td>
                  <td className={valueTone(item.unrealized_pnl)}>{money(item.unrealized_pnl, 4)}</td>
                  <td>{money(snapshot.settings.take_profit_usdt, 4)}</td>
                  <td className={takeProfitRemaining(item.unrealized_pnl, snapshot.settings.take_profit_usdt) === 0 ? "positive" : ""}>
                    {money(takeProfitRemaining(item.unrealized_pnl, snapshot.settings.take_profit_usdt), 4)}
                  </td>
                  <td>{pct(takeProfitProgress(item.unrealized_pnl, snapshot.settings.take_profit_usdt), 2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {snapshot.positions.length === 0 && <div className="empty">当前没有从交易所读取到合约持仓。</div>}
      </section>

      <section className="panel wide">
        <div className="section-title">
          <Boxes size={18} />
          <h2>期现反向套利监控</h2>
        </div>
        <ReverseCashCarryTable rows={ready} mode="ready" />
        {ready.length === 0 && <div className="empty">暂无满足借币校验的期现反向可开仓机会。</div>}
      </section>
    </div>
  );
}

function ReverseCashCarryTable({ rows, mode }: { rows: CashCarryOpportunity[]; mode: "ready" | "blocked" }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>排名</th>
            <th>交易所</th>
            <th>币种</th>
            <th>现货卖价</th>
            <th>合约买价</th>
            <th>合约折价</th>
            <th>资金费率</th>
            <th>名义本金</th>
            <th>预估保证金</th>
            <th>杠杆</th>
            <th>现货 24h 量</th>
            <th>合约 24h 量</th>
            <th>收敛收益</th>
            <th>资金费收入</th>
            <th>开平手续费</th>
            <th>可借数量</th>
            <th>借币日利率</th>
            <th>预估借币成本</th>
            <th>借币期限/风险</th>
            <th>净利预估</th>
            <th>最大安全本金</th>
            <th>{mode === "ready" ? "状态" : "不能开仓原因"}</th>
            <th>更新时间</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((item, index) => (
            <tr key={`${item.exchange}-${item.symbol}`}>
              <td>{index + 1}</td>
              <td>{item.exchange}</td>
              <td className="strong">{item.symbol}</td>
              <td>{money(item.spot_price, 6)}</td>
              <td>{money(item.perp_price, 6)}</td>
              <td className={valueTone(item.basis_pct)}>{pct(item.basis_pct)}</td>
              <td>{pct(item.funding_rate_pct)}</td>
              <td>{money(item.notional_usdt, 2)}</td>
              <td>{money(item.margin_required_usdt, 2)}</td>
              <td>{money(item.leverage, 1)}x</td>
              <td>{money(item.spot_volume_24h_usdt, 0)}</td>
              <td>{money(item.perp_volume_24h_usdt, 0)}</td>
              <td className={valueTone(item.estimated_basis_profit)}>{money(item.estimated_basis_profit, 4)}</td>
              <td className={valueTone(item.estimated_funding_income)}>{money(item.estimated_funding_income, 4)}</td>
              <td>{money(item.estimated_open_close_fee, 4)}</td>
              <td>{item.borrow_available_qty ? qty(item.borrow_available_qty) : "-"}</td>
              <td>{item.borrow_daily_rate_pct ? pct(item.borrow_daily_rate_pct) : "-"}</td>
              <td>{item.estimated_borrow_cost ? money(item.estimated_borrow_cost, 4) : "-"}</td>
              <td>{borrowText(item)}</td>
              <td className={valueTone(item.estimated_net_profit)}>{money(item.estimated_net_profit, 4)}</td>
              <td>{item.max_safe_notional_usdt ? money(item.max_safe_notional_usdt, 2) : "-"}</td>
              <td>{item.blocked_reasons.length > 0 ? item.blocked_reasons.join(" / ") : "满足条件"}</td>
              <td>{dateTimeMs(item.updated_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function borrowText(item: CashCarryOpportunity): string {
  const parts = [item.borrow_term, ...(item.borrow_risk_tags ?? [])].filter(Boolean);
  return parts.length > 0 ? parts.join(" / ") : "-";
}
