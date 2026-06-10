CREATE TABLE IF NOT EXISTS exchanges (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS exchange_symbols (
    id BIGSERIAL PRIMARY KEY,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    base_asset TEXT NOT NULL,
    quote_asset TEXT NOT NULL,
    contract_type TEXT NOT NULL,
    price_precision INTEGER NOT NULL,
    quantity_precision INTEGER NOT NULL,
    min_quantity NUMERIC(38, 18) NOT NULL,
    min_notional NUMERIC(38, 18) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE(exchange, symbol)
);

CREATE TABLE IF NOT EXISTS spot_transfer_routes (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    from_exchange TEXT NOT NULL,
    to_exchange TEXT NOT NULL,
    chain TEXT NOT NULL,
    deposit_enabled BOOLEAN NOT NULL,
    withdraw_enabled BOOLEAN NOT NULL,
    withdraw_fee NUMERIC(38, 18) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE(symbol, from_exchange, to_exchange, chain)
);

CREATE TABLE IF NOT EXISTS trade_pairs (
    id UUID PRIMARY KEY,
    symbol TEXT NOT NULL,
    long_exchange TEXT NOT NULL,
    short_exchange TEXT NOT NULL,
    quantity NUMERIC(38, 18) NOT NULL,
    status TEXT NOT NULL,
    open_reason TEXT NOT NULL,
    close_reason TEXT,
    opened_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ,
    reconcile_status TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS orders (
    id UUID PRIMARY KEY,
    trade_pair_id UUID NOT NULL REFERENCES trade_pairs(id),
    exchange TEXT NOT NULL,
    exchange_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    position_side TEXT NOT NULL,
    price NUMERIC(38, 18) NOT NULL,
    quantity NUMERIC(38, 18) NOT NULL,
    filled_quantity NUMERIC(38, 18) NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    raw_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE(exchange, exchange_order_id)
);

CREATE TABLE IF NOT EXISTS fills (
    id UUID PRIMARY KEY,
    order_id UUID NOT NULL REFERENCES orders(id),
    exchange TEXT NOT NULL,
    exchange_fill_id TEXT NOT NULL,
    price NUMERIC(38, 18) NOT NULL,
    quantity NUMERIC(38, 18) NOT NULL,
    fee NUMERIC(38, 18) NOT NULL,
    fee_asset TEXT NOT NULL,
    realized_pnl NUMERIC(38, 18),
    raw_payload JSONB NOT NULL,
    filled_at TIMESTAMPTZ NOT NULL,
    UNIQUE(exchange, exchange_fill_id)
);

CREATE TABLE IF NOT EXISTS funding_payments (
    id UUID PRIMARY KEY,
    trade_pair_id UUID NOT NULL REFERENCES trade_pairs(id),
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    amount NUMERIC(38, 18) NOT NULL,
    raw_payload JSONB NOT NULL,
    paid_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS pnl_snapshots (
    id BIGSERIAL PRIMARY KEY,
    trade_pair_id UUID NOT NULL REFERENCES trade_pairs(id),
    long_unrealized_pnl NUMERIC(38, 18) NOT NULL,
    short_unrealized_pnl NUMERIC(38, 18) NOT NULL,
    open_fee NUMERIC(38, 18) NOT NULL,
    estimated_close_fee NUMERIC(38, 18) NOT NULL,
    realized_funding_net NUMERIC(38, 18) NOT NULL,
    current_net_profit NUMERIC(38, 18) NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS risk_events (
    id UUID PRIMARY KEY,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    action TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS settings_audit (
    id BIGSERIAL PRIMARY KEY,
    settings JSONB NOT NULL,
    changed_by TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

