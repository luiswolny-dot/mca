-- ===========================================================================
-- Meme-Coin Alpha — PostgreSQL schema (PostgreSQL 16 + TimescaleDB 2.x)
--
-- Design decisions
-- ----------------
-- 1. TimescaleDB hypertables for the two high-volume tables (market_snapshots,
--    swap_events). Meme-coin tick data is 10-50M rows/month at moderate
--    coverage; plain B-tree indexes on a regular table degrade badly once the
--    index no longer fits in RAM. Compression after 7 days gives ~12x.
--
-- 2. NUMERIC, never DOUBLE PRECISION, for money. A float rounding error in a
--    PnL aggregate that reconciles against on-chain balances is a debugging
--    session nobody wants.
--
-- 3. Token amounts are NUMERIC(78,0) — base units, integral. 78 digits covers
--    uint256.
--
-- 4. `fills` and `risk_events` are APPEND-ONLY. No UPDATE, no DELETE, enforced
--    by trigger. Positions are derived state and may be rebuilt from fills at
--    any time; that is what makes reconciliation against the chain possible.
--
-- 5. Every table that feeds the ML pipeline carries `as_of` separately from
--    `created_at`. `as_of` is when the information was true; `created_at` is
--    when we learned it. Training on `created_at` reintroduces look-ahead bias
--    through the back door.
-- ===========================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE chain_t          AS ENUM ('solana','ethereum','base','bsc');
CREATE TYPE trade_mode_t     AS ENUM ('backtest','paper','live');
CREATE TYPE side_t           AS ENUM ('buy','sell');
CREATE TYPE position_status_t AS ENUM
    ('pending_entry','open','scaling_out','pending_exit','closed','failed','stuck');
CREATE TYPE risk_state_t     AS ENUM ('normal','derisk','halt','locked');

-- ---------------------------------------------------------------------------
-- Reference data
-- ---------------------------------------------------------------------------
CREATE TABLE tokens (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chain           chain_t      NOT NULL,
    address         TEXT         NOT NULL,
    symbol          TEXT,
    name            TEXT,
    decimals        SMALLINT     NOT NULL DEFAULT 9,
    created_on_chain_at TIMESTAMPTZ,
    deployer        TEXT,
    first_seen_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (chain, address)
);
CREATE INDEX idx_tokens_first_seen ON tokens (first_seen_at DESC);
CREATE INDEX idx_tokens_deployer   ON tokens (deployer) WHERE deployer IS NOT NULL;

CREATE TABLE pools (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    token_id        BIGINT      NOT NULL REFERENCES tokens(id),
    chain           chain_t     NOT NULL,
    dex             TEXT        NOT NULL,
    pool_address    TEXT        NOT NULL,
    quote_mint      TEXT        NOT NULL,
    fee_bps         INT         NOT NULL DEFAULT 30,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chain, pool_address)
);

-- ---------------------------------------------------------------------------
-- Security snapshots — one row per re-check, never overwritten.
-- Keeping the history is what lets us answer "was the mint authority live at
-- the moment we bought?" instead of "is it live now?".
-- ---------------------------------------------------------------------------
CREATE TABLE security_reports (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    token_id                BIGINT      NOT NULL REFERENCES tokens(id),
    as_of                   TIMESTAMPTZ NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    mint_authority_revoked  BOOLEAN,
    freeze_authority_revoked BOOLEAN,
    contract_renounced      BOOLEAN,
    is_proxy_upgradeable    BOOLEAN,
    has_blacklist_function  BOOLEAN,
    liquidity_usd           NUMERIC(24,6),
    lp_locked_pct           REAL,
    lp_burned               BOOLEAN,
    lp_lock_expiry          TIMESTAMPTZ,
    buy_tax_bps             INT,
    sell_tax_bps            INT,
    transfer_pausable       BOOLEAN,
    simulated_sell_ok       BOOLEAN,
    holder_count            INT,
    top1_holder_pct         REAL,
    top10_holder_pct        REAL,
    dev_holding_pct         REAL,
    bundled_supply_pct      REAL,
    insider_cluster_pct     REAL,
    deployer_prior_rugs     INT,
    raw                     JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX idx_secrep_token_asof ON security_reports (token_id, as_of DESC);

-- ---------------------------------------------------------------------------
-- Market data (hypertables)
-- ---------------------------------------------------------------------------
CREATE TABLE market_snapshots (
    as_of           TIMESTAMPTZ NOT NULL,
    token_id        BIGINT      NOT NULL,
    price_usd       NUMERIC(36,18) NOT NULL,
    liquidity_usd   NUMERIC(24,6),
    fdv_usd         NUMERIC(24,6),
    volume_5m_usd   NUMERIC(24,6),
    volume_1h_usd   NUMERIC(24,6),
    volume_24h_usd  NUMERIC(24,6),
    buys_5m         INT,
    sells_5m        INT,
    unique_buyers_5m INT,
    holder_count    INT,
    reserve_base    NUMERIC(78,0),
    reserve_quote   NUMERIC(78,0),
    source          TEXT NOT NULL,
    PRIMARY KEY (token_id, as_of, source)
);
SELECT create_hypertable('market_snapshots','as_of', chunk_time_interval => INTERVAL '1 day');
ALTER TABLE market_snapshots SET (timescaledb.compress,
    timescaledb.compress_segmentby = 'token_id');
SELECT add_compression_policy('market_snapshots', INTERVAL '7 days');

CREATE TABLE swap_events (
    as_of           TIMESTAMPTZ NOT NULL,
    token_id        BIGINT      NOT NULL,
    pool_id         BIGINT,
    tx_hash         TEXT        NOT NULL,
    block_or_slot   BIGINT      NOT NULL,
    wallet          TEXT        NOT NULL,
    side            side_t      NOT NULL,
    base_amount     NUMERIC(78,0) NOT NULL,
    quote_amount_usd NUMERIC(24,6) NOT NULL,
    price_usd       NUMERIC(36,18) NOT NULL,
    PRIMARY KEY (token_id, as_of, tx_hash)
);
SELECT create_hypertable('swap_events','as_of', chunk_time_interval => INTERVAL '1 day');
CREATE INDEX idx_swap_wallet ON swap_events (wallet, as_of DESC);

-- Labelled wallet cohorts. `pnl_30d_usd` is recomputed nightly from swap_events;
-- a wallet's "smart money" status is an empirical, decaying property, not a tag
-- someone typed in once.
CREATE TABLE wallet_labels (
    wallet          TEXT        NOT NULL,
    chain           chain_t     NOT NULL,
    label           TEXT        NOT NULL,   -- smart_money | whale | insider | mev | bot
    pnl_30d_usd     NUMERIC(24,6),
    win_rate_30d    REAL,
    trades_30d      INT,
    score           REAL        NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (wallet, chain, label)
);
CREATE INDEX idx_wallet_labels_score ON wallet_labels (label, score DESC);

CREATE TABLE social_snapshots (
    as_of           TIMESTAMPTZ NOT NULL,
    token_id        BIGINT      NOT NULL,
    platform        TEXT        NOT NULL,
    mentions_15m    INT,
    unique_authors_15m INT,
    weighted_reach  DOUBLE PRECISION,
    sentiment       REAL,
    bot_ratio       REAL,
    PRIMARY KEY (token_id, as_of, platform)
);
SELECT create_hypertable('social_snapshots','as_of', chunk_time_interval => INTERVAL '1 day');

-- ---------------------------------------------------------------------------
-- Decisions — every scored candidate, including the rejected ones.
-- Storing rejections is not optional: the rejection histogram is the primary
-- diagnostic for a silently broken data source, and the rejected set is the
-- control group for any counterfactual analysis.
-- ---------------------------------------------------------------------------
CREATE TABLE signals (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    token_id        BIGINT      NOT NULL REFERENCES tokens(id),
    as_of           TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    mode            trade_mode_t NOT NULL,
    score_total     REAL        NOT NULL,
    components      JSONB       NOT NULL,
    weights         JSONB       NOT NULL,
    gates           JSONB       NOT NULL,
    gates_passed    BOOLEAN     NOT NULL,
    failed_gates    TEXT[]      NOT NULL DEFAULT '{}',
    win_probability REAL,
    expected_value_bps REAL,
    model_version   TEXT,
    strategy_hash   TEXT        NOT NULL,
    acted_on        BOOLEAN     NOT NULL DEFAULT FALSE,
    rejection_reason TEXT,
    correlation_id  UUID        NOT NULL
);
CREATE INDEX idx_signals_asof     ON signals (as_of DESC);
CREATE INDEX idx_signals_acted    ON signals (acted_on, as_of DESC);
CREATE INDEX idx_signals_failed   ON signals USING GIN (failed_gates);

-- ---------------------------------------------------------------------------
-- Positions & fills
-- ---------------------------------------------------------------------------
CREATE TABLE positions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_id            BIGINT      NOT NULL REFERENCES tokens(id),
    signal_id           BIGINT      REFERENCES signals(id),
    mode                trade_mode_t NOT NULL,
    status              position_status_t NOT NULL,
    strategy_hash       TEXT        NOT NULL,
    opened_at           TIMESTAMPTZ NOT NULL,
    closed_at           TIMESTAMPTZ,
    base_amount         NUMERIC(78,0) NOT NULL DEFAULT 0,
    original_base_amount NUMERIC(78,0) NOT NULL DEFAULT 0,
    avg_entry_price_usd NUMERIC(36,18) NOT NULL DEFAULT 0,
    quote_invested_usd  NUMERIC(24,6) NOT NULL DEFAULT 0,
    realized_pnl_usd    NUMERIC(24,6) NOT NULL DEFAULT 0,
    fees_paid_usd       NUMERIC(24,6) NOT NULL DEFAULT 0,
    high_water_price_usd NUMERIC(36,18),
    stop_price_usd      NUMERIC(36,18),
    break_even_armed    BOOLEAN NOT NULL DEFAULT FALSE,
    ladder              JSONB   NOT NULL DEFAULT '[]'::jsonb,
    entry_score         REAL,
    entry_win_prob      REAL,
    exit_reason         TEXT,
    correlation_id      UUID    NOT NULL
);
CREATE INDEX idx_positions_open ON positions (status, mode) WHERE status IN ('open','scaling_out');
CREATE INDEX idx_positions_time ON positions (opened_at DESC);

CREATE TABLE fills (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    position_id     UUID        NOT NULL REFERENCES positions(id),
    intent_id       UUID        NOT NULL,
    idempotency_key TEXT        NOT NULL UNIQUE,   -- the double-buy guard
    token_id        BIGINT      NOT NULL REFERENCES tokens(id),
    side            side_t      NOT NULL,
    mode            trade_mode_t NOT NULL,
    base_amount     NUMERIC(78,0) NOT NULL,
    quote_amount_usd NUMERIC(24,6) NOT NULL,
    price_usd       NUMERIC(36,18) NOT NULL,
    slippage_bps    REAL,
    gas_or_fee_usd  NUMERIC(24,6) NOT NULL DEFAULT 0,
    tx_hash         TEXT,
    block_or_slot   BIGINT,
    latency_ms      INT,
    filled_at       TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_fills_position ON fills (position_id, filled_at);

-- Append-only enforcement.
CREATE OR REPLACE FUNCTION deny_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'table % is append-only (attempted %)', TG_TABLE_NAME, TG_OP;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER fills_append_only BEFORE UPDATE OR DELETE ON fills
    FOR EACH ROW EXECUTE FUNCTION deny_mutation();

-- ---------------------------------------------------------------------------
-- Risk state — the circuit breaker must survive a restart.
-- ---------------------------------------------------------------------------
CREATE TABLE risk_state (
    id              SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),  -- singleton
    state           risk_state_t NOT NULL DEFAULT 'normal',
    reason          TEXT,
    equity_usd      NUMERIC(24,6) NOT NULL DEFAULT 0,
    peak_equity_usd NUMERIC(24,6) NOT NULL DEFAULT 0,
    changed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    unlocked_by     TEXT,
    unlock_note     TEXT
);
INSERT INTO risk_state (id) VALUES (1) ON CONFLICT DO NOTHING;

CREATE TABLE risk_events (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    event           TEXT        NOT NULL,
    old_state       risk_state_t,
    new_state       risk_state_t,
    detail          JSONB       NOT NULL DEFAULT '{}'::jsonb
);
CREATE TRIGGER risk_events_append_only BEFORE UPDATE OR DELETE ON risk_events
    FOR EACH ROW EXECUTE FUNCTION deny_mutation();

-- Daily equity, the input to every performance metric.
CREATE TABLE equity_curve (
    day             DATE PRIMARY KEY,
    mode            trade_mode_t NOT NULL,
    opening_usd     NUMERIC(24,6) NOT NULL,
    closing_usd     NUMERIC(24,6) NOT NULL,
    realized_pnl_usd NUMERIC(24,6) NOT NULL,
    fees_usd        NUMERIC(24,6) NOT NULL,
    n_trades        INT NOT NULL DEFAULT 0
);

-- Shadow-trading record: the gate that guards live execution.
CREATE TABLE shadow_records (
    strategy_hash   TEXT PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL,
    paper_trades    INT NOT NULL DEFAULT 0,
    last_config_change_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    promoted_at     TIMESTAMPTZ,
    promoted_by     TEXT
);

CREATE TABLE model_registry (
    version         TEXT PRIMARY KEY,
    trained_at      TIMESTAMPTZ NOT NULL,
    auc             REAL,
    brier           REAL,
    n_train         INT,
    passed          BOOLEAN NOT NULL DEFAULT FALSE,
    artifact_path   TEXT,
    report          JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- ---------------------------------------------------------------------------
-- Convenience views
-- ---------------------------------------------------------------------------
CREATE VIEW v_open_positions AS
SELECT p.*, t.symbol, t.chain, t.address
FROM positions p JOIN tokens t ON t.id = p.token_id
WHERE p.status IN ('open','scaling_out');

CREATE VIEW v_gate_rejections_24h AS
SELECT unnest(failed_gates) AS gate, count(*) AS n
FROM signals
WHERE as_of > now() - INTERVAL '24 hours' AND NOT gates_passed
GROUP BY 1 ORDER BY n DESC;

CREATE VIEW v_trade_pnl AS
SELECT
    p.id, t.symbol, t.chain, p.mode, p.opened_at, p.closed_at,
    p.quote_invested_usd,
    p.realized_pnl_usd,
    CASE WHEN p.quote_invested_usd > 0
         THEN p.realized_pnl_usd / p.quote_invested_usd ELSE 0 END AS return_pct,
    p.exit_reason, p.entry_score, p.entry_win_prob
FROM positions p JOIN tokens t ON t.id = p.token_id
WHERE p.status = 'closed';
