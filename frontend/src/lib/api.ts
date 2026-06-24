import type { BotSettings, CredentialsOverview, ExchangeCredentialInput, ExchangeName, RealtimeSnapshot, TradeHistory } from "../types/api";

export async function fetchSnapshot(): Promise<RealtimeSnapshot> {
  const response = await fetch("/api/snapshot", { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`snapshot request failed: ${response.status}`);
  }
  return parseJson<RealtimeSnapshot>(response, "主控台快照");
}

export async function saveSettings(settings: BotSettings): Promise<BotSettings> {
  const response = await fetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
  if (!response.ok) {
    throw new Error(`settings save failed: ${response.status}`);
  }
  return response.json();
}

export async function fetchTrades(): Promise<TradeHistory[]> {
  const response = await fetch("/api/trades", { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`trades request failed: ${response.status}`);
  }
  return parseJson<TradeHistory[]>(response, "做单历史");
}

export async function fetchCredentials(): Promise<CredentialsOverview> {
  const response = await fetch("/api/credentials", { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`credentials request failed: ${response.status}`);
  }
  return parseJson<CredentialsOverview>(response, "API 管理");
}

export async function saveExchangeCredentials(exchange: ExchangeName, payload: ExchangeCredentialInput): Promise<CredentialsOverview> {
  const response = await fetch(`/api/credentials/exchanges/${exchange}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(await errorText(response, "credentials save failed"));
  }
  return fetchCredentials();
}

export async function deleteExchangeCredentials(exchange: ExchangeName): Promise<CredentialsOverview> {
  const response = await fetch(`/api/credentials/exchanges/${exchange}`, { method: "DELETE" });
  if (!response.ok) {
    throw new Error(await errorText(response, "credentials delete failed"));
  }
  return fetchCredentials();
}

export async function testExchangeCredentials(exchange: ExchangeName): Promise<string> {
  const response = await fetch(`/api/credentials/exchanges/${exchange}/test`, { method: "POST" });
  if (!response.ok) {
    throw new Error(await errorText(response, "credentials test failed"));
  }
  const body = await response.json();
  return body.message ?? "连接测试通过";
}

export async function saveDeepSeekCredentials(payload: { api_key?: string; base_url?: string; model?: string }): Promise<CredentialsOverview> {
  const response = await fetch("/api/credentials/ai/deepseek", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(await errorText(response, "deepseek save failed"));
  }
  return fetchCredentials();
}

export async function deleteDeepSeekCredentials(): Promise<CredentialsOverview> {
  const response = await fetch("/api/credentials/ai/deepseek", { method: "DELETE" });
  if (!response.ok) {
    throw new Error(await errorText(response, "deepseek delete failed"));
  }
  return fetchCredentials();
}

export async function saveMt4Credentials(payload: { bridge_token?: string }): Promise<CredentialsOverview> {
  const response = await fetch("/api/credentials/mt4", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(await errorText(response, "mt4 save failed"));
  }
  return fetchCredentials();
}

export async function deleteMt4Credentials(): Promise<CredentialsOverview> {
  const response = await fetch("/api/credentials/mt4", { method: "DELETE" });
  if (!response.ok) {
    throw new Error(await errorText(response, "mt4 delete failed"));
  }
  return fetchCredentials();
}

async function errorText(response: Response, fallback: string): Promise<string> {
  try {
    const body = await response.json();
    if (body.detail) return String(body.detail);
  } catch {
    // ignore JSON parse failures and use the fallback below.
  }
  return `${fallback}: ${response.status}`;
}

async function parseJson<T>(response: Response, label: string): Promise<T> {
  const contentType = response.headers.get("content-type") || "";
  const text = await response.text();
  if (!contentType.includes("application/json")) {
    throw new Error(`${label}返回了非 JSON 数据，后端或登录代理正在重启`);
  }
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new Error(`${label}JSON 解析失败，已保留上一份实时数据`);
  }
}

export function createRealtimeSocket(
  onMessage: (snapshot: RealtimeSnapshot) => void,
  onError: () => void,
): WebSocket {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${window.location.host}/ws/realtime`);
  socket.onmessage = (event) => {
    try {
      onMessage(JSON.parse(event.data));
    } catch {
      onError();
    }
  };
  socket.onerror = onError;
  return socket;
}
