import { Activity, Banknote, Layers } from "lucide-react";
import type { RealtimeSnapshot } from "../types/api";
import { dateTime, money, pct, qty, takeProfitProgress, takeProfitRemaining, valueTone } from "../lib/format";

type Props = {
  snapshot: RealtimeSnapshot;
};

export function Dashboard({ snapshot }: Props) {
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
          <h2>当前持仓</h2>
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
      </section>

      <section className="panel wide">
        <div className="section-title">
          <Activity size={18} />
          <h2>套利组合仪表盘</h2>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>币种</th>
                <th>做多所</th>
                <th>做空所</th>
                <th>数量</th>
                <th>杠杆</th>
                <th>多单浮盈亏</th>
                <th>空单浮盈亏</th>
                <th>开仓手续费</th>
                <th>预估平仓费</th>
                <th>资金费率已收支</th>
                <th>资金费率预估</th>
                <th>开仓价差</th>
                <th>当前价差</th>
                <th>补仓</th>
                <th>当前净利润</th>
                <th>止盈目标</th>
                <th>距止盈</th>
                <th>止盈进度</th>
              </tr>
            </thead>
            <tbody>
              {snapshot.dashboard.map((item) => (
                <tr key={item.trade_pair_id}>
                  <td className="strong">{item.symbol}</td>
                  <td>{item.long_exchange}</td>
                  <td>{item.short_exchange}</td>
                  <td>{qty(item.long_quantity)}</td>
                  <td>{money(item.leverage, 1)}x</td>
                  <td className={valueTone(item.long_unrealized_pnl)}>{money(item.long_unrealized_pnl, 4)}</td>
                  <td className={valueTone(item.short_unrealized_pnl)}>{money(item.short_unrealized_pnl, 4)}</td>
                  <td>{money(item.open_fee, 4)}</td>
                  <td>{money(item.estimated_close_fee, 4)}</td>
                  <td className={valueTone(item.realized_funding_net)}>{money(item.realized_funding_net, 4)}</td>
                  <td className={valueTone(item.estimated_funding_net)}>{money(item.estimated_funding_net, 4)}</td>
                  <td>{pct(item.entry_spread_pct)}</td>
                  <td>{pct(item.current_spread_pct)}</td>
                  <td>{item.add_count}</td>
                  <td className={valueTone(item.current_net_profit)}>{money(item.current_net_profit, 4)}</td>
                  <td>{money(snapshot.settings.take_profit_usdt, 4)}</td>
                  <td className={takeProfitRemaining(item.current_net_profit, snapshot.settings.take_profit_usdt) === 0 ? "positive" : ""}>
                    {money(takeProfitRemaining(item.current_net_profit, snapshot.settings.take_profit_usdt), 4)}
                  </td>
                  <td>{pct(takeProfitProgress(item.current_net_profit, snapshot.settings.take_profit_usdt), 2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
