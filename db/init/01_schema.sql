-- Gielinomics schema.
-- Applied automatically by the postgres container on first start (docker-entrypoint-initdb.d).

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ---------------------------------------------------------------------------
-- Reference data
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS items (
    id         INTEGER PRIMARY KEY,
    name       TEXT,
    examine    TEXT,
    members    BOOLEAN,
    buy_limit  INTEGER,                -- per 4 hours; null when the item has no published limit
    value      BIGINT,
    lowalch    BIGINT,
    highalch   BIGINT,
    icon       TEXT,
    -- A stub row is inserted the first time an unknown ID appears in a price feed,
    -- before /mapping has caught up. Everything above is therefore nullable.
    is_stub    BOOLEAN NOT NULL DEFAULT false,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Price history
--
-- No foreign keys to items(id) from any hypertable. New item IDs appear in the
-- price feeds the moment they become tradeable, up to 24h before the daily
-- /mapping sync knows about them, and one unknown ID would fail the whole batch.
-- ---------------------------------------------------------------------------

-- Aggregated bars at any granularity. step_seconds is part of the key so live 5m
-- ingest, hourly ingest, daily /timeseries backfill and future rollups coexist.
CREATE TABLE IF NOT EXISTS price_series (
    item_id      INTEGER NOT NULL,
    step_seconds INTEGER NOT NULL,      -- 300 | 3600 | 86400
    bucket_ts    TIMESTAMPTZ NOT NULL,  -- start of the window
    avg_high     NUMERIC,
    avg_low      NUMERIC,
    high_volume  BIGINT,
    low_volume   BIGINT,
    source       TEXT NOT NULL,         -- '5m' | '1h' | 'timeseries'
    PRIMARY KEY (item_id, step_seconds, bucket_ts)
);
SELECT create_hypertable('price_series', 'bucket_ts', if_not_exists => true);
CREATE INDEX IF NOT EXISTS price_series_step_bucket_idx ON price_series (step_seconds, bucket_ts DESC);

-- Latest observed trades, for spread and liquidity work.
-- APPEND ONLY WHEN THE TRADE TIMESTAMPS CHANGE. /latest returns the same high/low
-- until a trade actually occurs; writing every 60s regardless would be ~5.3M rows/day,
-- almost all of them byte-identical duplicates.
CREATE TABLE IF NOT EXISTS price_latest (
    item_id     INTEGER NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    high        BIGINT,
    high_time   TIMESTAMPTZ,
    low         BIGINT,
    low_time    TIMESTAMPTZ,
    PRIMARY KEY (item_id, observed_at)
);
SELECT create_hypertable('price_latest', 'observed_at', if_not_exists => true);

-- ---------------------------------------------------------------------------
-- Ingest audit
--
-- Written on EVERY poll attempt, successful or not. This is the only thing that
-- distinguishes "the market was quiet" from "the worker was dead", and it cannot
-- be reconstructed after the fact. It exists from the first commit for that reason.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ingest_runs (
    id            BIGSERIAL PRIMARY KEY,
    source        TEXT NOT NULL,           -- '5m' | '1h' | 'latest' | 'mapping' | 'timeseries'
    target_bucket TIMESTAMPTZ,             -- the window being fetched, when the source has one
    attempted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ,
    outcome       TEXT NOT NULL,           -- 'running' | 'ok' | 'http_error' | 'parse_error' | 'db_error' | 'unknown_error'
    rows_written  INTEGER,
    detail        TEXT
);
CREATE INDEX IF NOT EXISTS ingest_runs_source_bucket_idx ON ingest_runs (source, target_bucket);
CREATE INDEX IF NOT EXISTS ingest_runs_attempted_idx ON ingest_runs (attempted_at DESC);

-- ---------------------------------------------------------------------------
-- Accounts
--
-- Gated behind the scope decision in plan.md. If account tracking is cut from v1,
-- everything below this line goes with it.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS players (
    id              BIGSERIAL PRIMARY KEY,
    display_name    TEXT NOT NULL,
    normalised_name TEXT NOT NULL UNIQUE,   -- lowercased, spaces normalised
    account_type    TEXT NOT NULL,
    tracked         BOOLEAN NOT NULL DEFAULT true,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Names change, and a rename must not split one player's history in two.
-- All name-keyed API lookups resolve through here to a stable player_id.
CREATE TABLE IF NOT EXISTS player_names (
    player_id  BIGINT NOT NULL REFERENCES players(id),
    name       TEXT NOT NULL,
    normalised TEXT NOT NULL,
    seen_from  TIMESTAMPTZ NOT NULL DEFAULT now(),
    seen_to    TIMESTAMPTZ,                 -- null = current
    PRIMARY KEY (player_id, normalised, seen_from)
);
CREATE INDEX IF NOT EXISTS player_names_normalised_idx ON player_names (normalised);

CREATE TABLE IF NOT EXISTS hiscore_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    player_id       BIGINT NOT NULL REFERENCES players(id),
    captured_at     TIMESTAMPTZ NOT NULL,   -- first capture with this content
    last_seen_at    TIMESTAMPTZ NOT NULL,   -- most recent capture, bumped on match
    payload         JSONB NOT NULL,
    content_hash    BYTEA NOT NULL,
    mapping_version INTEGER NOT NULL,       -- which index->name mapping decoded this payload
    -- captured_at deliberately NOT in this key. Including it would make every row unique
    -- by construction and the dedup would never fire.
    UNIQUE (player_id, content_hash)
);

CREATE TABLE IF NOT EXISTS skill_samples (
    player_id   BIGINT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    skill       SMALLINT NOT NULL,          -- positional index; decode via mapping_version
    rank        INTEGER,
    level       SMALLINT,
    xp          BIGINT,
    PRIMARY KEY (player_id, captured_at, skill)
);
SELECT create_hypertable('skill_samples', 'captured_at', if_not_exists => true);

-- ---------------------------------------------------------------------------
-- API users and alerting
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS api_users (
    id         BIGSERIAL PRIMARY KEY,
    label      TEXT NOT NULL,
    token_hash BYTEA NOT NULL UNIQUE,
    enabled    BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS alert_rules (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL REFERENCES api_users(id),
    kind        TEXT NOT NULL,              -- 'margin' | 'volume' | 'xp_milestone'
    config      JSONB NOT NULL,
    webhook_url TEXT NOT NULL,
    enabled     BOOLEAN NOT NULL DEFAULT true,
    last_fired  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Defence in depth. The application validates the host on write; this stops a
    -- bad migration or a direct INSERT turning the alert dispatcher into an open
    -- request relay pointed at the internal network.
    CONSTRAINT alert_rules_webhook_host CHECK (
        webhook_url ~ '^https://(canary\.|ptb\.)?discord(app)?\.com/api/webhooks/'
    )
);
