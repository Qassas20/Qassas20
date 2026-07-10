-- Reality Calibration Layer schema (UAT addendum Section 7).
--
-- This is a self-contained schema for the calibration layer. It includes a
-- minimal `buckets` and `trades` core (the "Phase 1" tables the addendum
-- extends) plus the calibration-specific columns and tables. If a Phase 1
-- schema already exists, apply only the `ALTER TABLE` additions and the two
-- new CREATE TABLE statements.

PRAGMA foreign_keys = ON;

-- --------------------------------------------------------------------------
-- Core (Phase 1) tables
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS buckets (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    risk_tier    TEXT,                 -- e.g. 'low' | 'medium' | 'high'
    max_drawdown REAL,                 -- risk-tier drawdown limit ($ or fraction)
    daily_stop   REAL,                 -- max daily loss for this bucket
    created_at   TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id             INTEGER PRIMARY KEY,
    bucket_id      INTEGER NOT NULL REFERENCES buckets(id),
    symbol         TEXT NOT NULL,
    direction      TEXT NOT NULL DEFAULT 'long',  -- 'long' | 'short'
    qty            REAL NOT NULL,
    entry_time     TEXT,
    exit_time      TEXT,
    entry_price    REAL,               -- signal entry price (frictionless)
    exit_price     REAL,               -- signal exit price (frictionless)

    -- Reality Calibration Layer additions (Section 7)
    raw_pnl            REAL,           -- exactly what Alpaca paper reports
    adjusted_pnl       REAL,           -- after friction; THE predictor of live
    modeled_slippage   REAL,           -- total per-share slippage across legs
    fees               REAL,           -- modeled SEC + FINRA TAF (sell side)
    latency_ms         INTEGER,        -- signal -> fill delay applied
    haircut_qty        REAL,           -- shares rejected by the liquidity cap
    regime             TEXT,           -- 'trending' | 'choppy'
    signal_price       REAL,           -- entry signal price (fill-quality ref)
    modeled_fill_price REAL            -- entry modeled fill price
);

CREATE INDEX IF NOT EXISTS idx_trades_bucket ON trades(bucket_id);
CREATE INDEX IF NOT EXISTS idx_trades_regime ON trades(regime);

-- --------------------------------------------------------------------------
-- Calibration config (tunable, not hardcoded) — Section 7
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS calibration_config (
    id                  INTEGER PRIMARY KEY,
    impact_k            REAL,          -- market impact constant
    liquidity_cap_pct   REAL,          -- e.g. 0.02
    latency_ms          INTEGER,       -- e.g. 400
    half_spread_min     REAL,          -- always cross at least this
    sec_fee_per_dollar  REAL,          -- SEC Section 31 fee per $ of proceeds
    finra_taf_per_share REAL,          -- FINRA TAF per share sold
    finra_taf_max       REAL,          -- FINRA TAF per-trade cap
    updated_at          TEXT
);

-- --------------------------------------------------------------------------
-- Shadow-live gap tracking (Section 6 + Section 7)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS live_vs_paper (
    id                 INTEGER PRIMARY KEY,
    bucket_id          INTEGER REFERENCES buckets(id),
    symbol             TEXT,
    timestamp          TEXT,
    paper_adjusted_pnl REAL,
    live_actual_pnl    REAL,
    gap                REAL            -- live_actual - paper_adjusted
);

CREATE INDEX IF NOT EXISTS idx_lvp_bucket ON live_vs_paper(bucket_id);
