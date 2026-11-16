-- Day Trading System — PostgreSQL schema
-- Run once on first deploy (cloud/db.py handles auto-migration)

CREATE TABLE IF NOT EXISTS scans (
    id            SERIAL PRIMARY KEY,
    scan_type     TEXT NOT NULL,
    run_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    date          DATE NOT NULL,
    hits          INTEGER NOT NULL,
    symbols       TEXT[],
    duration_ms   INTEGER,
    raw_output    TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id              SERIAL PRIMARY KEY,
    date            DATE NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             NUMERIC NOT NULL,
    setup           TEXT,

    entry_price     NUMERIC,
    stop_price      NUMERIC,
    target_price    NUMERIC,
    risk_per_share  NUMERIC,
    max_loss        NUMERIC,
    reward_risk     NUMERIC,

    mc_target_pct   NUMERIC,
    mc_stop_pct     NUMERIC,
    mc_neither_pct  NUMERIC,
    mc_history_wins INTEGER,
    mc_history_total INTEGER,

    risk_verdict    TEXT,
    risk_reasons    TEXT[],
    fingerprint     TEXT NOT NULL,

    status          TEXT NOT NULL DEFAULT 'pending',
    telegram_msg_id BIGINT,
    sent_at         TIMESTAMPTZ,
    decided_at      TIMESTAMPTZ,
    decision        TEXT,

    broker_order_id TEXT,
    placed_at       TIMESTAMPTZ,
    fill_price      NUMERIC,
    fill_qty        NUMERIC,
    filled_at       TIMESTAMPTZ,

    pnl             NUMERIC,
    pnl_r           NUMERIC,
    outcome         TEXT,

    paper           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS trade_pairs (
    id          SERIAL PRIMARY KEY,
    entry_id    INTEGER REFERENCES trades(id),
    exit_id     INTEGER REFERENCES trades(id),
    symbol      TEXT NOT NULL,
    date        DATE NOT NULL,
    entry_price NUMERIC,
    exit_price  NUMERIC,
    qty         NUMERIC,
    pnl         NUMERIC,
    pnl_r       NUMERIC,
    outcome     TEXT,
    hold_minutes INTEGER
);

CREATE TABLE IF NOT EXISTS daily_snapshots (
    id              SERIAL PRIMARY KEY,
    date            DATE NOT NULL UNIQUE,
    cash            NUMERIC NOT NULL,
    positions_value NUMERIC NOT NULL,
    total_equity    NUMERIC NOT NULL,
    trades_today    INTEGER DEFAULT 0,
    wins_today      INTEGER DEFAULT 0,
    losses_today    INTEGER DEFAULT 0,
    pnl_today       NUMERIC DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id          SERIAL PRIMARY KEY,
    run_type    TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status      TEXT NOT NULL DEFAULT 'running',
    steps       JSONB,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS scan_hits (
    id          SERIAL PRIMARY KEY,
    date        DATE NOT NULL,
    symbol      TEXT NOT NULL,
    scan_type   TEXT NOT NULL DEFAULT 'orb',
    scan_time   TEXT,
    hit_data    JSONB,
    status      TEXT NOT NULL DEFAULT 'pending',
    skip_reason TEXT,
    processed_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(date);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_setup ON trades(setup);
CREATE INDEX IF NOT EXISTS idx_trade_pairs_date ON trade_pairs(date);
CREATE INDEX IF NOT EXISTS idx_daily_snapshots_date ON daily_snapshots(date);
CREATE INDEX IF NOT EXISTS idx_scan_hits_date_status ON scan_hits(date, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_scan_hits_date_symbol ON scan_hits(date, symbol, scan_type);
