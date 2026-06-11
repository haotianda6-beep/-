export type ExchangeName = "OKX" | "GATE" | "BITGET" | "BYBIT" | "BINANCE";

export type ExchangeBalance = {
  exchange: ExchangeName;
  equity_usdt: string;
  available_usdt: string;
  margin_used_usdt: string;
  updated_at: string;
};

export type PositionSnapshot = {
  exchange: ExchangeName;
  symbol: string;
  side: "long" | "short";
  quantity: string;
  entry_price: string;
  mark_price: string;
  leverage: string;
  unrealized_pnl: string;
  liquidation_price: string | null;
};

export type CashCarryPositionRow = {
  exchange: ExchangeName;
  symbol: string;
  status: "matched" | "mismatch" | "spot_only" | "perp_only";
  spot_quantity: string;
  spot_entry_price: string;
  spot_price: string;
  spot_unrealized_pnl: string;
  perp_side: "short" | "long" | "none";
  perp_contracts: string;
  perp_base_quantity: string;
  contract_size: string;
  perp_entry_price: string;
  perp_mark_price: string;
  leverage: string;
  perp_unrealized_pnl: string;
  estimated_funding_rate_pct: string;
  estimated_funding_income: string;
  estimated_open_fee: string;
  estimated_close_fee: string;
  current_net_profit: string;
  quantity_gap: string;
  basis_pct: string;
  add_count: number;
  add_notional_usdt: string;
  next_add_trigger_basis_pct: string | null;
  updated_at: string;
};

export type CashCarryOpportunity = {
  exchange: ExchangeName;
  symbol: string;
  spot_price: string;
  perp_price: string;
  basis_pct: string;
  funding_rate_pct: string;
  quantity: string;
  spot_volume_24h_usdt: string;
  perp_volume_24h_usdt: string;
  estimated_basis_profit: string;
  estimated_funding_income: string;
  estimated_open_close_fee: string;
  estimated_net_profit: string;
  max_safe_notional_usdt: string | null;
  notional_usdt: string;
  margin_required_usdt: string;
  leverage: string;
  blocked_reasons: string[];
  data_source: "mock" | "live";
  updated_at: string;
};

export type TradeHistory = {
  trade_pair_id: string;
  strategy_type: "cash_carry" | "mt4_spread";
  symbol: string;
  quantity: string;
  opened_at: string;
  closed_at: string | null;
  long_exchange: ExchangeName;
  short_exchange: ExchangeName;
  long_open_price: string;
  long_close_price: string | null;
  short_open_price: string;
  short_close_price: string | null;
  actual_fee: string;
  total_pnl: string;
  long_pnl: string;
  short_pnl: string;
  funding_net: string;
  actual_net_profit: string;
  close_reason: string | null;
  long_order_ids: string[];
  short_order_ids: string[];
  reconcile_status: "pending" | "verified" | "mismatch";
};

export type BotSettings = {
  order_notional_usdt: string;
  max_total_notional_usdt: string;
  max_symbol_notional_usdt: string;
  default_leverage: string;
  max_leverage: string;
  margin_mode: "isolated" | "cross";
  cash_carry_min_basis_pct: string;
  cash_carry_close_basis_pct: string;
  cash_carry_min_funding_rate_pct: string;
  cash_carry_min_volume_usdt: string;
  mt4_spread_enabled: boolean;
  mt4_min_spread_pct: string;
  mt4_min_net_profit_usdt: string;
  mt4_notional_usdt: string;
  mt4_default_leverage: string;
  mt4_max_quote_age_seconds: string;
  take_profit_usdt: string;
  stop_loss_usdt: string;
  max_slippage_pct: string;
  min_funding_net_usdt: string;
  max_add_count: number;
  add_trigger_spread_pct: string;
  single_exchange_max_notional_usdt: string;
  symbol_blacklist: string[];
  exchange_blacklist: ExchangeName[];
  cash_carry_enabled: boolean;
  cash_carry_auto_open_enabled: boolean;
  cash_carry_auto_close_enabled: boolean;
  cash_carry_auto_transfer_enabled: boolean;
  cash_carry_auto_trade_enabled: boolean;
  manual_confirm_required: boolean;
  ai_risk_monitor_enabled: boolean;
  emergency_close_enabled: boolean;
};

export type RiskEvent = {
  id: string;
  severity: "info" | "warning" | "critical";
  title: string;
  detail: string;
  action: string;
  created_at: string;
};

export type ExchangeCredentialStatus = {
  exchange: ExchangeName;
  configured: boolean;
  missing_fields: string[];
  live_data_enabled: boolean;
  trading_enabled: boolean;
  order_execution_enabled: boolean;
  read_only_mode: boolean;
  source: "vault" | "env" | "mixed" | "missing";
  masked_api_key: string | null;
  updated_at: string | null;
  last_test_ok: boolean | null;
  last_test_message: string | null;
  last_test_at: string | null;
  use_testnet: boolean;
  use_demo: boolean;
};

export type ExchangeCredentialInput = {
  api_key?: string;
  api_secret?: string;
  passphrase?: string;
  use_testnet?: boolean;
  use_demo?: boolean;
};

export type DeepSeekCredentialStatus = {
  provider: "deepseek";
  configured: boolean;
  source: "vault" | "env" | "missing";
  masked_api_key: string | null;
  base_url: string;
  model: string;
  updated_at: string | null;
};

export type Mt4CredentialStatus = {
  configured: boolean;
  source: "vault" | "env" | "missing";
  masked_token: string | null;
  updated_at: string | null;
};

export type CredentialsOverview = {
  exchanges: ExchangeCredentialStatus[];
  ai: {
    deepseek: DeepSeekCredentialStatus;
  };
  mt4: Mt4CredentialStatus;
  server_public_ip: string;
};

export type AIInsight = {
  provider: string;
  model: string;
  status: "ready" | "disabled" | "not_configured" | "error";
  content: string;
  updated_at: string;
  next_refresh_at: string | null;
};

export type RealtimeSnapshot = {
  balances: ExchangeBalance[];
  positions: PositionSnapshot[];
  cash_carry_opportunities: CashCarryOpportunity[];
  cash_carry_candidates: CashCarryOpportunity[];
  cash_carry_positions: CashCarryPositionRow[];
  mt4_spread_opportunities: Mt4SpreadOpportunity[];
  mt4_spread_candidates: Mt4SpreadOpportunity[];
  trades: TradeHistory[];
  settings: BotSettings;
  risk_events: RiskEvent[];
  credential_status: ExchangeCredentialStatus[];
  ai_insight: AIInsight;
  data_source: "mock" | "live";
  generated_at: string;
};

export type Mt4SpreadOpportunity = {
  instrument: string;
  instrument_type: "stock" | "commodity";
  mt4_symbol: string;
  exchange: ExchangeName;
  exchange_symbol: string;
  long_venue: string;
  short_venue: string;
  mt4_bid: string;
  mt4_ask: string;
  exchange_bid: string;
  exchange_ask: string;
  spread_pct: string;
  notional_usdt: string;
  margin_required_usdt: string;
  leverage: string;
  mt4_contract_size: string;
  mt4_lots: string;
  hedge_base_quantity: string;
  estimated_exchange_funding_net: string;
  estimated_mt4_overnight_net: string;
  estimated_open_close_fee: string;
  estimated_net_profit: string;
  blocked_reasons: string[];
  data_source: "mock" | "live";
  updated_at: string;
};
