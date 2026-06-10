import { KeyRound, RefreshCw, Save, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import {
  deleteDeepSeekCredentials,
  deleteExchangeCredentials,
  deleteMt4Credentials,
  fetchCredentials,
  saveDeepSeekCredentials,
  saveExchangeCredentials,
  saveMt4Credentials,
  testExchangeCredentials,
} from "../lib/api";
import type { CredentialsOverview, DeepSeekCredentialStatus, ExchangeCredentialStatus, ExchangeName, Mt4CredentialStatus } from "../types/api";

const exchanges: ExchangeName[] = ["OKX", "GATE", "BITGET", "BYBIT", "BINANCE"];

export function ApiCredentialsPage() {
  const [overview, setOverview] = useState<CredentialsOverview | null>(null);
  const [error, setError] = useState("");

  async function refresh() {
    try {
      setOverview(await fetchCredentials());
      setError("");
    } catch (reason) {
      setError(String(reason));
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  if (!overview) {
    return <section className="panel wide">正在读取 API 配置状态...</section>;
  }

  return (
    <section className="panel wide">
      <div className="section-title actions-title">
        <div className="section-title">
          <KeyRound size={18} />
          <h2>API 管理</h2>
        </div>
        <button className="primary-button" onClick={refresh} title="刷新状态">
          <RefreshCw size={16} />
          刷新
        </button>
      </div>
      <div className="security-note">
        <strong>服务器出口 IP：{overview.server_public_ip || "读取失败"}</strong>
        <span>API 密钥只会加密保存到服务器，页面不会回显已保存的明文密钥。交易所 API 必须关闭提现权限。</span>
      </div>
      {error && <div className="save-state negative">{error}</div>}

      <div className="api-grid">
        {exchanges.map((exchange) => {
          const status = overview.exchanges.find((item) => item.exchange === exchange);
          return status ? <ExchangeEditor key={exchange} status={status} onOverview={setOverview} /> : null;
        })}
      </div>

      <div className="api-grid two">
        <DeepSeekEditor status={overview.ai.deepseek} onOverview={setOverview} />
        <Mt4Editor status={overview.mt4} onOverview={setOverview} />
      </div>
    </section>
  );
}

function ExchangeEditor({ status, onOverview }: { status: ExchangeCredentialStatus; onOverview: (overview: CredentialsOverview) => void }) {
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [useTestnet, setUseTestnet] = useState(status.use_testnet);
  const [useDemo, setUseDemo] = useState(status.use_demo);
  const [message, setMessage] = useState("");

  useEffect(() => {
    setUseTestnet(status.use_testnet);
    setUseDemo(status.use_demo);
  }, [status.use_testnet, status.use_demo]);

  async function save() {
    setMessage("保存中...");
    try {
      onOverview(await saveExchangeCredentials(status.exchange, { api_key: apiKey, api_secret: apiSecret, passphrase, use_testnet: useTestnet, use_demo: useDemo }));
      setApiKey("");
      setApiSecret("");
      setPassphrase("");
      setMessage("已保存");
    } catch (reason) {
      setMessage(String(reason));
    }
  }

  async function test() {
    setMessage("测试中...");
    try {
      setMessage(await testExchangeCredentials(status.exchange));
      onOverview(await fetchCredentials());
    } catch (reason) {
      setMessage(String(reason));
      onOverview(await fetchCredentials());
    }
  }

  async function remove() {
    if (!window.confirm(`删除 ${status.exchange} 加密保存的 API 配置？`)) return;
    onOverview(await deleteExchangeCredentials(status.exchange));
    setMessage("已删除加密配置");
  }

  const needsPassphrase = status.exchange === "OKX" || status.exchange === "BITGET";

  return (
    <div className="api-section">
      <CredentialHead title={status.exchange} configured={status.configured} source={status.source} masked={status.masked_api_key} />
      <div className="settings-grid compact api-fields">
        <SecretField label="API Key" value={apiKey} onChange={setApiKey} />
        <SecretField label="API Secret" value={apiSecret} onChange={setApiSecret} />
        {needsPassphrase && <SecretField label="Passphrase" value={passphrase} onChange={setPassphrase} />}
      </div>
      <div className="checkbox-row api-switches">
        <label className="toggle">
          <input type="checkbox" checked={useTestnet} onChange={(event) => setUseTestnet(event.target.checked)} />
          <span>测试网</span>
        </label>
        {(status.exchange === "OKX" || status.exchange === "BITGET") && (
          <label className="toggle">
            <input type="checkbox" checked={useDemo} onChange={(event) => setUseDemo(event.target.checked)} />
            <span>模拟盘</span>
          </label>
        )}
      </div>
      <ActionRow onSave={save} onTest={test} onDelete={remove} />
      <CredentialMeta status={status} message={message} />
    </div>
  );
}

function DeepSeekEditor({ status, onOverview }: { status: DeepSeekCredentialStatus; onOverview: (overview: CredentialsOverview) => void }) {
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState(status.base_url || "https://api.deepseek.com");
  const [model, setModel] = useState(status.model || "deepseek-chat");
  const [message, setMessage] = useState("");

  async function save() {
    setMessage("保存中...");
    try {
      onOverview(await saveDeepSeekCredentials({ api_key: apiKey, base_url: baseUrl, model }));
      setApiKey("");
      setMessage("已保存");
    } catch (reason) {
      setMessage(String(reason));
    }
  }

  async function remove() {
    if (!window.confirm("删除加密保存的 DeepSeek 配置？")) return;
    onOverview(await deleteDeepSeekCredentials());
    setMessage("已删除加密配置");
  }

  return (
    <div className="api-section">
      <CredentialHead title="DeepSeek" configured={status.configured} source={status.source} masked={status.masked_api_key} />
      <div className="settings-grid compact api-fields">
        <SecretField label="API Key" value={apiKey} onChange={setApiKey} />
        <TextField label="Base URL" value={baseUrl} onChange={setBaseUrl} />
        <TextField label="Model" value={model} onChange={setModel} />
      </div>
      <div className="button-row">
        <button className="primary-button" onClick={save}><Save size={16} />保存</button>
        <button onClick={remove}><Trash2 size={16} />删除</button>
      </div>
      {message && <div className="api-message">{message}</div>}
    </div>
  );
}

function Mt4Editor({ status, onOverview }: { status: Mt4CredentialStatus; onOverview: (overview: CredentialsOverview) => void }) {
  const [token, setToken] = useState("");
  const [message, setMessage] = useState("");

  async function save() {
    setMessage("保存中...");
    try {
      onOverview(await saveMt4Credentials({ bridge_token: token }));
      setToken("");
      setMessage("已保存");
    } catch (reason) {
      setMessage(String(reason));
    }
  }

  async function remove() {
    if (!window.confirm("删除加密保存的 MT4 Bridge Token？")) return;
    onOverview(await deleteMt4Credentials());
    setMessage("已删除加密配置");
  }

  return (
    <div className="api-section">
      <CredentialHead title="MT4 Bridge" configured={status.configured} source={status.source} masked={status.masked_token} />
      <div className="settings-grid compact api-fields">
        <SecretField label="Bridge Token" value={token} onChange={setToken} />
      </div>
      <div className="button-row">
        <button className="primary-button" onClick={save}><Save size={16} />保存</button>
        <button onClick={remove}><Trash2 size={16} />删除</button>
      </div>
      {message && <div className="api-message">{message}</div>}
    </div>
  );
}

function CredentialHead({ title, configured, source, masked }: { title: string; configured: boolean; source: string; masked: string | null }) {
  return (
    <div className="api-head">
      <div>
        <h3>{title}</h3>
        <span>{masked || "未保存 key"}</span>
      </div>
      <strong className={configured ? "positive" : "negative"}>{configured ? "已配置" : "未完整"}</strong>
      <small>{sourceLabel(source)}</small>
    </div>
  );
}

function CredentialMeta({ status, message }: { status: ExchangeCredentialStatus; message: string }) {
  return (
    <div className="api-message">
      {status.missing_fields.length > 0 && <span>缺少：{status.missing_fields.join(", ")}</span>}
      {status.last_test_message && <span>上次测试：{status.last_test_ok ? "通过" : "失败"}，{status.last_test_message}</span>}
      {message && <span>{message}</span>}
    </div>
  );
}

function ActionRow({ onSave, onTest, onDelete }: { onSave: () => void; onTest: () => void; onDelete: () => void }) {
  return (
    <div className="button-row">
      <button className="primary-button" onClick={onSave}><Save size={16} />保存</button>
      <button onClick={onTest}><RefreshCw size={16} />测试连接</button>
      <button onClick={onDelete}><Trash2 size={16} />删除</button>
    </div>
  );
}

function SecretField({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  return <TextField label={label} value={value} onChange={onChange} type="password" placeholder="留空则不覆盖已有值" />;
}

function TextField({ label, value, onChange, type = "text", placeholder = "" }: { label: string; value: string; onChange: (value: string) => void; type?: string; placeholder?: string }) {
  return (
    <label className="field">
      <span>{label}</span>
      <input type={type} value={value} placeholder={placeholder} autoComplete="off" onChange={(event) => onChange(event.target.value)} />
    </label>
  );
}

function sourceLabel(source: string): string {
  if (source === "vault") return "加密存储";
  if (source === "env") return ".env 兼容";
  if (source === "mixed") return "混合来源";
  return "未配置";
}
