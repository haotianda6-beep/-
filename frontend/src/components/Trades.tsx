import { History } from "lucide-react";
import type { ReactNode } from "react";
import type { TradeHistory } from "../types/api";
import { dateTime, money, qty, valueTone } from "../lib/format";

type Props = {
  trades: TradeHistory[];
  strategy: TradeHistory["strategy_type"];
  title?: string;
  emptyText?: string;
};

export function Trades({ trades, strategy, title = "做单历史", emptyText = "还没有经过交易所成交回执核验的真实历史单。" }: Props) {
  if (strategy === "cash_carry") {
    return <CashCarryTrades trades={trades} title={title} emptyText={emptyText} />;
  }
  if (strategy === "reverse_cash_carry") {
    return <ReverseCashCarryTrades trades={trades} title={title} emptyText={emptyText} />;
  }
  if (strategy === "mt4_spread") {
    return <Mt4Trades trades={trades} title={title} emptyText={emptyText} />;
  }
  return <PerpSpreadTrades trades={trades} title={title} emptyText={emptyText} />;
}

function PerpSpreadTrades({ trades, title, emptyText }: Omit<Props, "strategy">) {
  return (
    <TradePanel title={title} emptyText={emptyText} isEmpty={trades.length === 0}>
        <table>
          <thead>
            <tr>
              <th>币种</th>
              <th>数量</th>
              <th>开仓时间</th>
              <th>平仓时间</th>
              <th>做多所</th>
              <th>做空所</th>
              <th>多开仓价</th>
              <th>多平仓价</th>
              <th>空开仓价</th>
              <th>空平仓价</th>
              <th>实际手续费</th>
              <th>多空总盈亏</th>
              <th>多单盈亏</th>
              <th>空单盈亏</th>
              <th>资金费率收支</th>
              <th>实际净利</th>
              <th>平仓原因</th>
              <th>对账</th>
            </tr>
          </thead>
          <tbody>
            {trades.map((item) => (
              <tr key={item.trade_pair_id}>
                <td className="strong">{item.symbol}</td>
                <td>{qty(item.quantity)}</td>
                <td>{dateTime(item.opened_at)}</td>
                <td>{dateTime(item.closed_at)}</td>
                <td>{item.long_exchange}</td>
                <td>{item.short_exchange}</td>
                <td>{money(item.long_open_price, 6)}</td>
                <td>{item.long_close_price ? money(item.long_close_price, 6) : "-"}</td>
                <td>{money(item.short_open_price, 6)}</td>
                <td>{item.short_close_price ? money(item.short_close_price, 6) : "-"}</td>
                <td>{money(item.actual_fee, 4)}</td>
                <td className={valueTone(item.total_pnl)}>{money(item.total_pnl, 4)}</td>
                <td className={valueTone(item.long_pnl)}>{money(item.long_pnl, 4)}</td>
                <td className={valueTone(item.short_pnl)}>{money(item.short_pnl, 4)}</td>
                <td className={valueTone(item.funding_net)}>{money(item.funding_net, 4)}</td>
                <td className={valueTone(item.actual_net_profit)}>{money(item.actual_net_profit, 4)}</td>
                <td>{item.close_reason ?? "-"}</td>
                <td>{item.reconcile_status}</td>
              </tr>
            ))}
          </tbody>
        </table>
    </TradePanel>
  );
}

function CashCarryTrades({ trades, title, emptyText }: Omit<Props, "strategy">) {
  return (
    <TradePanel title={title} emptyText={emptyText} isEmpty={trades.length === 0}>
      <table>
        <thead>
          <tr>
            <th>币种</th>
            <th>数量</th>
            <th>开仓时间</th>
            <th>平仓时间</th>
            <th>交易所</th>
            <th>现货买入价</th>
            <th>现货卖出价</th>
            <th>合约做空价</th>
            <th>合约平空价</th>
            <th>实际手续费</th>
            <th>现货+合约盈亏</th>
            <th>现货盈亏</th>
            <th>合约盈亏</th>
            <th>资金费率收支</th>
            <th>实际净利</th>
            <th>平仓原因</th>
            <th>对账</th>
          </tr>
        </thead>
        <tbody>{trades.map((item) => <CashCarryRow key={item.trade_pair_id} item={item} />)}</tbody>
      </table>
    </TradePanel>
  );
}

function ReverseCashCarryTrades({ trades, title, emptyText }: Omit<Props, "strategy">) {
  return (
    <TradePanel title={title} emptyText={emptyText} isEmpty={trades.length === 0}>
      <table>
        <thead>
          <tr>
            <th>币种</th>
            <th>数量</th>
            <th>开仓时间</th>
            <th>平仓时间</th>
            <th>交易所</th>
            <th>借币卖出现货价</th>
            <th>买回现货价</th>
            <th>合约做多价</th>
            <th>合约平多价</th>
            <th>实际手续费</th>
            <th>现货+合约盈亏</th>
            <th>现货盈亏</th>
            <th>合约盈亏</th>
            <th>资金费率收支</th>
            <th>实际净利</th>
            <th>平仓原因</th>
            <th>对账</th>
          </tr>
        </thead>
        <tbody>{trades.map((item) => <CashCarryRow key={item.trade_pair_id} item={item} />)}</tbody>
      </table>
    </TradePanel>
  );
}

function Mt4Trades({ trades, title, emptyText }: Omit<Props, "strategy">) {
  return (
    <TradePanel title={title} emptyText={emptyText} isEmpty={trades.length === 0}>
      <table>
        <thead>
          <tr>
            <th>品种</th>
            <th>数量</th>
            <th>开仓时间</th>
            <th>平仓时间</th>
            <th>MT4/外部市场</th>
            <th>交易所</th>
            <th>外部开仓价</th>
            <th>外部平仓价</th>
            <th>交易所开仓价</th>
            <th>交易所平仓价</th>
            <th>实际手续费</th>
            <th>总盈亏</th>
            <th>外部腿盈亏</th>
            <th>交易所腿盈亏</th>
            <th>资金费率收支</th>
            <th>实际净利</th>
            <th>平仓原因</th>
            <th>对账</th>
          </tr>
        </thead>
        <tbody>{trades.map((item) => <PerpSpreadRow key={item.trade_pair_id} item={item} />)}</tbody>
      </table>
    </TradePanel>
  );
}

function TradePanel({ title, emptyText, isEmpty, children }: { title?: string; emptyText?: string; isEmpty: boolean; children: ReactNode }) {
  return (
    <section className="panel wide">
      <div className="section-title">
        <History size={18} />
        <h2>{title}</h2>
      </div>
      <div className="table-wrap">{children}</div>
      {isEmpty && <div className="empty">{emptyText}</div>}
    </section>
  );
}

function PerpSpreadRow({ item }: { item: TradeHistory }) {
  return (
    <tr>
      <td className="strong">{item.symbol}</td>
      <td>{qty(item.quantity)}</td>
      <td>{dateTime(item.opened_at)}</td>
      <td>{dateTime(item.closed_at)}</td>
      <td>{item.long_exchange}</td>
      <td>{item.short_exchange}</td>
      <td>{money(item.long_open_price, 6)}</td>
      <td>{item.long_close_price ? money(item.long_close_price, 6) : "-"}</td>
      <td>{money(item.short_open_price, 6)}</td>
      <td>{item.short_close_price ? money(item.short_close_price, 6) : "-"}</td>
      <td>{money(item.actual_fee, 4)}</td>
      <td className={valueTone(item.total_pnl)}>{money(item.total_pnl, 4)}</td>
      <td className={valueTone(item.long_pnl)}>{money(item.long_pnl, 4)}</td>
      <td className={valueTone(item.short_pnl)}>{money(item.short_pnl, 4)}</td>
      <td className={valueTone(item.funding_net)}>{money(item.funding_net, 4)}</td>
      <td className={valueTone(item.actual_net_profit)}>{money(item.actual_net_profit, 4)}</td>
      <td>{item.close_reason ?? "-"}</td>
      <td>{item.reconcile_status}</td>
    </tr>
  );
}

function CashCarryRow({ item }: { item: TradeHistory }) {
  return (
    <tr>
      <td className="strong">{item.symbol}</td>
      <td>{qty(item.quantity)}</td>
      <td>{dateTime(item.opened_at)}</td>
      <td>{dateTime(item.closed_at)}</td>
      <td>{item.long_exchange}</td>
      <td>{money(item.long_open_price, 6)}</td>
      <td>{item.long_close_price ? money(item.long_close_price, 6) : "-"}</td>
      <td>{money(item.short_open_price, 6)}</td>
      <td>{item.short_close_price ? money(item.short_close_price, 6) : "-"}</td>
      <td>{money(item.actual_fee, 4)}</td>
      <td className={valueTone(item.total_pnl)}>{money(item.total_pnl, 4)}</td>
      <td className={valueTone(item.long_pnl)}>{money(item.long_pnl, 4)}</td>
      <td className={valueTone(item.short_pnl)}>{money(item.short_pnl, 4)}</td>
      <td className={valueTone(item.funding_net)}>{money(item.funding_net, 4)}</td>
      <td className={valueTone(item.actual_net_profit)}>{money(item.actual_net_profit, 4)}</td>
      <td>{item.close_reason ?? "-"}</td>
      <td>{item.reconcile_status}</td>
    </tr>
  );
}
