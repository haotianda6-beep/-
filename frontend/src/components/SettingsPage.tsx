import { Save, SlidersHorizontal } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { saveSettings } from "../lib/api";
import type { BotSettings, ExchangeName } from "../types/api";

type Props = {
  settings: BotSettings;
  onSaved: (settings: BotSettings) => void;
};

type DecimalKey =
  | "order_notional_usdt"
  | "max_total_notional_usdt"
  | "max_symbol_notional_usdt"
  | "default_leverage"
  | "max_leverage"
  | "min_open_spread_pct"
  | "cash_carry_min_basis_pct"
  | "cash_carry_close_basis_pct"
  | "cash_carry_min_funding_rate_pct"
  | "cash_carry_min_volume_usdt"
  | "mt4_min_spread_pct"
  | "mt4_min_net_profit_usdt"
  | "mt4_notional_usdt"
  | "mt4_default_leverage"
  | "mt4_max_quote_age_seconds"
  | "target_close_spread_pct"
  | "take_profit_usdt"
  | "stop_loss_usdt"
  | "max_slippage_pct"
  | "min_24h_volume_usdt"
  | "min_funding_net_usdt"
  | "add_notional_usdt"
  | "add_trigger_spread_pct"
  | "single_exchange_max_notional_usdt";

const sharedDecimalFields: Array<{ key: DecimalKey; label: string; suffix: string }> = [
  { key: "order_notional_usdt", label: "单笔下单金额", suffix: "USDT" },
  { key: "max_total_notional_usdt", label: "最大总仓位", suffix: "USDT" },
  { key: "max_symbol_notional_usdt", label: "单币最大仓位", suffix: "USDT" },
  { key: "default_leverage", label: "默认杠杆", suffix: "x" },
  { key: "max_leverage", label: "最大杠杆", suffix: "x" },
  { key: "single_exchange_max_notional_usdt", label: "单所最大暴露", suffix: "USDT" },
  { key: "add_trigger_spread_pct", label: "补仓走扩触发", suffix: "%" },
  { key: "take_profit_usdt", label: "止盈金额", suffix: "USDT" },
  { key: "stop_loss_usdt", label: "止损金额", suffix: "USDT" },
  { key: "max_slippage_pct", label: "最大滑点", suffix: "%" },
  { key: "min_funding_net_usdt", label: "最小净收益", suffix: "USDT" },
];

const perpDecimalFields: Array<{ key: DecimalKey; label: string; suffix: string }> = [
  { key: "min_open_spread_pct", label: "最小开仓价差", suffix: "%" },
  { key: "target_close_spread_pct", label: "目标平仓价差", suffix: "%" },
  { key: "min_24h_volume_usdt", label: "最低 24h 成交量", suffix: "USDT" },
];

const cashCarryDecimalFields: Array<{ key: DecimalKey; label: string; suffix: string }> = [
  { key: "cash_carry_min_basis_pct", label: "期现最小正基差", suffix: "%" },
  { key: "cash_carry_close_basis_pct", label: "期现收敛平仓基差", suffix: "%" },
  { key: "cash_carry_min_funding_rate_pct", label: "期现最低资金费率", suffix: "%" },
  { key: "cash_carry_min_volume_usdt", label: "期现最低24h成交量", suffix: "USDT" },
];

const mt4SpreadDecimalFields: Array<{ key: DecimalKey; label: string; suffix: string }> = [
  { key: "mt4_notional_usdt", label: "MT4价差名义本金", suffix: "USDT" },
  { key: "mt4_default_leverage", label: "MT4价差杠杆", suffix: "x" },
  { key: "mt4_min_spread_pct", label: "MT4最小价差", suffix: "%" },
  { key: "mt4_min_net_profit_usdt", label: "MT4最小净利", suffix: "USDT" },
  { key: "mt4_max_quote_age_seconds", label: "MT4报价过期", suffix: "秒" },
];

type ToggleField = { key: keyof BotSettings; label: string };

const toggleGroups: Array<{ title: string; toggles: ToggleField[] }> = [
  { title: "通用安全开关", toggles: [
      { key: "manual_confirm_required", label: "需要人工确认" },
      { key: "ai_risk_monitor_enabled", label: "AI 风险监控" },
      { key: "emergency_close_enabled", label: "紧急平仓开关" },
    ] },
  { title: "五所永续价差套利开关", toggles: [
      { key: "auto_open_enabled", label: "五所永续允许自动开仓" },
      { key: "auto_close_enabled", label: "五所永续允许自动平仓" },
    ] },
  { title: "各所期现正向套利开关", toggles: [
      { key: "cash_carry_enabled", label: "启用正向期现扫描" },
      { key: "cash_carry_auto_open_enabled", label: "正向期现允许自动开仓" },
      { key: "cash_carry_auto_close_enabled", label: "正向期现允许自动平仓" },
      { key: "cash_carry_auto_transfer_enabled", label: "正向期现允许自动划转" },
      { key: "cash_carry_auto_trade_enabled", label: "正向期现允许自动下单" },
    ] },
  { title: "MT4 与五所价差套利开关", toggles: [
      { key: "mt4_spread_enabled", label: "启用 MT4 价差扫描" },
    ] },
];

const exchanges: ExchangeName[] = ["OKX", "GATE", "BITGET", "BYBIT", "BINANCE"];

export function SettingsPage({ settings, onSaved }: Props) {
  const [draft, setDraft] = useState<BotSettings>(settings);
  const [status, setStatus] = useState("");
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    if (!dirty) setDraft(settings);
  }, [settings, dirty]);

  const symbolText = useMemo(() => draft.symbol_blacklist.join(", "), [draft.symbol_blacklist]);

  function updateDraft(next: BotSettings | ((current: BotSettings) => BotSettings)) {
    setDirty(true);
    setStatus("");
    setDraft(next);
  }

  function updateDecimal(key: DecimalKey, value: string) {
    updateDraft((current) => ({ ...current, [key]: value }));
  }

  function updateToggle(key: keyof BotSettings, checked: boolean) {
    updateDraft((current) => ({ ...current, [key]: checked }));
  }

  function updateExchangeBlacklist(exchange: ExchangeName, checked: boolean) {
    updateDraft((current) => {
      const next = new Set(current.exchange_blacklist);
      if (checked) next.add(exchange);
      else next.delete(exchange);
      return { ...current, exchange_blacklist: Array.from(next) };
    });
  }

  async function submit() {
    setStatus("saving");
    try {
      const saved = await saveSettings(draft);
      setDraft(saved);
      setDirty(false);
      onSaved(saved);
      setStatus("saved");
      window.setTimeout(() => setStatus(""), 1200);
    } catch (error) {
      setStatus("error");
    }
  }

  return (
    <section className="panel wide">
      <div className="section-title actions-title">
        <div className="section-title">
          <SlidersHorizontal size={18} />
          <h2>参数设置</h2>
        </div>
        <button className="primary-button" onClick={submit} title="保存参数">
          <Save size={16} />
          保存
        </button>
      </div>

      <SettingsGroup title="通用资金和风控参数" fields={sharedDecimalFields} draft={draft} onChange={updateDecimal} />
      <SettingsGroup title="五所永续价差套利参数" fields={perpDecimalFields} draft={draft} onChange={updateDecimal} />
      <SettingsGroup title="各所期现正向套利参数" fields={cashCarryDecimalFields} draft={draft} onChange={updateDecimal} />
      <SettingsGroup title="MT4 与五所价差套利参数" fields={mt4SpreadDecimalFields} draft={draft} onChange={updateDecimal} />

      <div className="settings-grid">
        <label className="field">
          <span>最大补仓次数</span>
          <div className="input-row">
            <input
              type="number"
              min="0"
              max="10"
              value={draft.max_add_count}
              onChange={(event) => updateDraft((current) => ({ ...current, max_add_count: Number(event.target.value) }))}
            />
            <small>次</small>
          </div>
        </label>

        <label className="field">
          <span>补仓金额</span>
          <div className="input-row">
            <input value={draft.add_notional_usdt} inputMode="decimal" onChange={(event) => updateDecimal("add_notional_usdt", event.target.value)} />
            <small>USDT</small>
          </div>
        </label>

        <label className="field">
          <span>保证金模式</span>
          <select
            value={draft.margin_mode}
            onChange={(event) => updateDraft((current) => ({ ...current, margin_mode: event.target.value as "isolated" | "cross" }))}
          >
            <option value="isolated">isolated</option>
            <option value="cross">cross</option>
          </select>
        </label>
      </div>

      <div className="toggle-groups">
        {toggleGroups.map((group) => (
          <ToggleGroup key={group.title} title={group.title} toggles={group.toggles} draft={draft} onToggle={updateToggle} />
        ))}
      </div>

      <div className="settings-grid compact">
        <label className="field wide-field">
          <span>币种黑名单</span>
          <input
            value={symbolText}
            placeholder="例如 DOGEUSDT, SOLUSDT"
            onChange={(event) =>
              updateDraft((current) => ({
                ...current,
                symbol_blacklist: event.target.value
                  .split(",")
                  .map((item) => item.trim().toUpperCase())
                  .filter(Boolean),
              }))
            }
          />
        </label>

        <div className="field wide-field">
          <span>交易所黑名单</span>
          <div className="checkbox-row">
            {exchanges.map((exchange) => (
              <label className="toggle" key={exchange}>
                <input
                  type="checkbox"
                  checked={draft.exchange_blacklist.includes(exchange)}
                  onChange={(event) => updateExchangeBlacklist(exchange, event.target.checked)}
                />
                <span>{exchange}</span>
              </label>
            ))}
          </div>
        </div>
      </div>

      {status === "saved" && <div className="save-state">参数已保存</div>}
      {status === "error" && <div className="save-state negative">保存失败，请检查参数格式</div>}
    </section>
  );
}

function ToggleGroup({ title, toggles, draft, onToggle }: {
  title: string; toggles: ToggleField[]; draft: BotSettings; onToggle: (key: keyof BotSettings, checked: boolean) => void;
}) {
  return (
    <div className="toggle-group">
      <h3 className="settings-heading">{title}</h3>
      <div className="toggle-grid">
        {toggles.map((toggle) => (
          <label className="toggle" key={String(toggle.key)}>
            <input
              type="checkbox"
              checked={Boolean(draft[toggle.key])}
              onChange={(event) => onToggle(toggle.key, event.target.checked)}
            />
            <span>{toggle.label}</span>
          </label>
        ))}
      </div>
    </div>
  );
}

function SettingsGroup({
  title,
  fields,
  draft,
  onChange,
}: {
  title: string;
  fields: Array<{ key: DecimalKey; label: string; suffix: string }>;
  draft: BotSettings;
  onChange: (key: DecimalKey, value: string) => void;
}) {
  return (
    <>
      <h3 className="settings-heading">{title}</h3>
      <div className="settings-grid">
        {fields.map((field) => (
          <label className="field" key={field.key}>
            <span>{field.label}</span>
            <div className="input-row">
              <input value={draft[field.key]} inputMode="decimal" onChange={(event) => onChange(field.key, event.target.value)} />
              <small>{field.suffix}</small>
            </div>
          </label>
        ))}
      </div>
    </>
  );
}
