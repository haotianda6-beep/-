import { Bot } from "lucide-react";
import type { AIInsight } from "../types/api";
import { dateTime } from "../lib/format";

type Props = {
  insight: AIInsight;
};

export function AiPanel({ insight }: Props) {
  return (
    <aside className="ai-panel">
      <div className="section-title">
        <Bot size={18} />
        <h2>AI 风控输出</h2>
      </div>
      <div className={`ai-status ${insight.status}`}>
        <span>{statusLabel(insight.status)}</span>
        <small>{insight.provider || "deepseek"} · {insight.model || "-"}</small>
      </div>
      <div className="ai-content">
        {insight.content.split("\n").map((line, index) => (
          <p key={`${index}-${line}`}>{line}</p>
        ))}
      </div>
      <div className="ai-meta">
        <span>更新：{dateTime(insight.updated_at)}</span>
        {insight.next_refresh_at && <span>下次：{dateTime(insight.next_refresh_at)}</span>}
      </div>
    </aside>
  );
}

function statusLabel(status: AIInsight["status"]): string {
  if (status === "ready") return "已连接";
  if (status === "disabled") return "已关闭";
  if (status === "error") return "调用异常";
  return "未配置";
}
