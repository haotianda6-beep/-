import { ShieldAlert } from "lucide-react";
import type { ExchangeCredentialStatus, RiskEvent } from "../types/api";
import { dateTime } from "../lib/format";

type Props = {
  events: RiskEvent[];
  credentials: ExchangeCredentialStatus[];
};

export function RiskPanel({ events, credentials }: Props) {
  return (
    <aside className="risk-panel">
      <div className="section-title">
        <ShieldAlert size={18} />
        <h2>AI 风险监控</h2>
      </div>
      {events.map((event) => (
        <div className={`risk-event ${event.severity}`} key={event.id}>
          <div className="risk-head">
            <strong>{event.title}</strong>
            <span>{dateTime(event.created_at)}</span>
          </div>
          <p>{event.detail}</p>
          <small>{event.action}</small>
        </div>
      ))}
      <div className="credential-block">
        <h3>API 配置状态</h3>
        {credentials.map((item) => (
          <div className="credential-row" key={item.exchange}>
            <strong>{item.exchange}</strong>
            <span className={item.configured ? "positive" : "negative"}>
              {item.configured ? "已配置" : `缺少 ${item.missing_fields.length} 项`}
            </span>
          </div>
        ))}
        <div className="switch-state">
          <span>实盘数据：{credentials[0]?.live_data_enabled ? "开启" : "关闭"}</span>
          <span>交易权限：{credentials[0]?.trading_enabled ? "开启" : "关闭"}</span>
          <span>下单执行：{credentials[0]?.order_execution_enabled ? "开启" : "关闭"}</span>
          <span>只读模式：{credentials[0]?.read_only_mode ? "开启" : "关闭"}</span>
        </div>
      </div>
    </aside>
  );
}
