import { Banknote, Landmark, Layers } from "lucide-react";
import { dateTime, dateTimeMs, money, pct, qty, takeProfitProgress, takeProfitRemaining, valueTone } from "../lib/format";
import type { CashCarryOpportunity, CashCarryPositionRow, ExchangeName, RealtimeSnapshot } from "../types/api";

const CASH_CARRY_EXCHANGES: ExchangeName[] = ["GATE", "BITGET"];

type Props = {
  opportunities: CashCarryOpportunity[];
  candidates: CashCarryOpportunity[];
};

export function CashCarry({ opportunities, candidates }: Props) {
  const ready = opportunities.filter((item) => CASH_CARRY_EXCHANGES.includes(item.exchange));
  const blocked = candidates.filter((item) => CASH_CARRY_EXCHANGES.includes(item.exchange));
  return (
    <div className="page-grid">
      <section className="panel wide">
        <div className="section-title">
          <Landmark size={18} />
          <h2>GATE / BITGET 期现正向套利机会</h2>
        </div>
        <CashCarryTable rows={ready} mode="ready" />
        {ready.length === 0 && <div className="empty">GATE / BITGET 暂无同时满足正资金费率和合约溢价阈值的期现机会。</div>}
      </section>

      <section className="panel wide">
        <div className="section-title">
          <Landmark size={18} />
          <h2>GATE / BITGET 候选与不能开仓原因</h2>
        </div>
        <CashCarryTable rows={blocked} mode="blocked" />
        {blocked.length === 0 && <div className="empty">GATE / BITGET 期现扫描后台加载中或暂无候选。</div>}
      </section>
    </div>
  );
}

export function CashCarryDashboard({ snapshot }: { snapshot: RealtimeSnapshot }) {
  const ready = snapshot.cash_carry_opportunities ?? [];
  const groups = snapshot.cash_carry_positions ?? [];
  const balances = snapshot.balances.filter((item) => CASH_CARRY_EXCHANGES.includes(item.exchange));
  const positions = snapshot.positions.filter((item) => CASH_CARRY_EXCHANGES.includes(item.exchange));
  return (
    <div className="page-grid">
      <section className="panel">
        <div className="section-title">
          <Banknote size={18} />
          <h2>GATE / BITGET 余额</h2>
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
              {balances.map((item) => (
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
          <h2>GATE / BITGET 期现正向持仓组合</h2>
        </div>
        <CashCarryPositionTable rows={groups} takeProfit={snapshot.settings.take_profit_usdt} />
        {groups.length === 0 && <div className="empty">当前没有读取到正向期现组合持仓。</div>}
      </section>

      <section className="panel wide">
        <div className="section-title">
          <Layers size={18} />
          <h2>合约持仓明细</h2>
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
              {positions.map((item) => (
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
        {positions.length === 0 && <div className="empty">当前没有从 GATE / BITGET 读取到合约持仓。</div>}
      </section>

      <section className="panel wide">
        <div className="section-title">
          <Landmark size={18} />
          <h2>GATE / BITGET 期现正向套利监控</h2>
        </div>
        <CashCarryTable rows={ready} mode="ready" />
        {ready.length === 0 && <div className="empty">暂无满足期现正向套利参数的可开仓机会。</div>}
      </section>
    </div>
  );
}

function CashCarryPositionTable({ rows, takeProfit }: { rows: CashCarryPositionRow[]; takeProfit: string }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>状态</th>
            <th>交易所</th>
            <th>币种</th>
            <th>现货数量</th>
            <th>现货成本价</th>
            <th>现货价格</th>
            <th>现货浮盈亏</th>
            <th>合约方向</th>
            <th>合约张数</th>
            <th>合约折算币数</th>
            <th>数量差</th>
            <th>合约开仓价</th>
            <th>合约标记价</th>
            <th>基差</th>
            <th>补仓金额</th>
            <th>补仓次数/下次基差</th>
            <th>合约浮盈亏</th>
            <th>资金费率估</th>
            <th>资金费收支估</th>
            <th>开仓手续费估</th>
            <th>平仓手续费估</th>
            <th>当前净利</th>
            <th>止盈目标</th>
            <th>距止盈</th>
            <th>止盈进度</th>
            <th>更新时间</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((item) => (
            <tr key={`${item.exchange}-${item.symbol}`}>
              <td className={item.status === "matched" ? "positive" : "negative"}>{positionStatus(item.status)}</td>
              <td>{item.exchange}</td>
              <td className="strong">{item.symbol}</td>
              <td>{qty(item.spot_quantity)}</td>
              <td>{money(item.spot_entry_price, 6)}</td>
              <td>{money(item.spot_price, 6)}</td>
              <td className={valueTone(item.spot_unrealized_pnl)}>{money(item.spot_unrealized_pnl, 4)}</td>
              <td>{item.perp_side === "short" ? "空" : item.perp_side === "long" ? "多" : "-"}</td>
              <td>{qty(item.perp_contracts)}</td>
              <td>{qty(item.perp_base_quantity)}</td>
              <td className={valueTone(item.quantity_gap)}>{qty(item.quantity_gap)}</td>
              <td>{money(item.perp_entry_price, 6)}</td>
              <td>{money(item.perp_mark_price, 6)}</td>
              <td className={valueTone(item.basis_pct)}>{pct(item.basis_pct)}</td>
              <td>{money(item.add_notional_usdt, 4)}</td>
              <td>{item.add_count ?? 0} 次 / {item.next_add_trigger_basis_pct ? pct(item.next_add_trigger_basis_pct) : "-"}</td>
              <td className={valueTone(item.perp_unrealized_pnl)}>{money(item.perp_unrealized_pnl, 4)}</td>
              <td className={valueTone(item.estimated_funding_rate_pct)}>{pct(item.estimated_funding_rate_pct)}</td>
              <td className={valueTone(item.estimated_funding_income)}>{money(item.estimated_funding_income, 4)}</td>
              <td>{money(item.estimated_open_fee, 4)}</td>
              <td>{money(item.estimated_close_fee, 4)}</td>
              <td className={valueTone(item.current_net_profit)}>{money(item.current_net_profit, 4)}</td>
              <td>{money(takeProfit, 4)}</td>
              <td className={takeProfitRemaining(item.current_net_profit, takeProfit) === 0 ? "positive" : ""}>
                {money(takeProfitRemaining(item.current_net_profit, takeProfit), 4)}
              </td>
              <td>{pct(takeProfitProgress(item.current_net_profit, takeProfit), 2)}</td>
              <td>{dateTimeMs(item.updated_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function positionStatus(status: CashCarryPositionRow["status"]) {
  if (status === "matched") return "已对齐";
  if (status === "spot_only") return "仅现货";
  if (status === "perp_only") return "仅合约";
  return "数量不一致";
}

function CashCarryTable({ rows, mode }: { rows: CashCarryOpportunity[]; mode: "ready" | "blocked" }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>排名</th>
            <th>交易所</th>
            <th>币种</th>
            <th>现货买价</th>
            <th>合约卖价</th>
            <th>合约溢价</th>
            <th>资金费率</th>
            <th>名义本金</th>
            <th>预估保证金</th>
            <th>杠杆</th>
            <th>现货 24h 量</th>
            <th>合约 24h 量</th>
            <th>价差收益</th>
            <th>资金费收入</th>
            <th>开平手续费</th>
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
              <td className={valueTone(item.funding_rate_pct)}>{pct(item.funding_rate_pct)}</td>
              <td>{money(item.notional_usdt, 2)}</td>
              <td>{money(item.margin_required_usdt, 2)}</td>
              <td>{money(item.leverage, 1)}x</td>
              <td>{money(item.spot_volume_24h_usdt, 0)}</td>
              <td>{money(item.perp_volume_24h_usdt, 0)}</td>
              <td className={valueTone(item.estimated_basis_profit)}>{money(item.estimated_basis_profit, 4)}</td>
              <td className={valueTone(item.estimated_funding_income)}>{money(item.estimated_funding_income, 4)}</td>
              <td>{money(item.estimated_open_close_fee, 4)}</td>
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
