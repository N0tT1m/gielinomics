-- Wiki structured data, synced weekly from the Bucket API.
--
-- Applied automatically by the postgres container on FIRST start only, like 01_schema.sql.
-- An existing deployment must apply this by hand:
--     docker compose exec -T postgres psql -U gielinomics -d gielinomics < db/init/02_wiki.sql
--
-- Everything here is near-static reference data and is replaced wholesale on each sync, so
-- surrogate keys are enough — there is no stable row identity upstream to preserve. Bucket
-- exposes no row id at all, only an implicit page_name, and one page legitimately carries
-- several rows.

-- ---------------------------------------------------------------------------
-- Equipment bonuses. The gear comparison surface.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS item_bonuses (
    id                   BIGSERIAL PRIMARY KEY,
    page_name            TEXT NOT NULL,
    -- Resolved against items(name) at load time, so gear joins to live prices by the same key
    -- the price API uses. Null for anything untradeable or not yet in the mapping.
    item_id              INTEGER,
    equipment_slot       TEXT,
    combat_style         TEXT,
    weapon_attack_speed  INTEGER,          -- game ticks
    weapon_attack_range  TEXT,
    stab_attack          INTEGER,
    slash_attack         INTEGER,
    crush_attack         INTEGER,
    range_attack         INTEGER,
    magic_attack         INTEGER,
    stab_defence         INTEGER,
    slash_defence        INTEGER,
    crush_defence        INTEGER,
    range_defence        INTEGER,
    magic_defence        INTEGER,
    strength_bonus       INTEGER,
    ranged_strength_bonus INTEGER,
    prayer_bonus         INTEGER,
    magic_damage_bonus   DOUBLE PRECISION,
    synced_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS item_bonuses_item_idx ON item_bonuses (item_id);
CREATE INDEX IF NOT EXISTS item_bonuses_slot_idx ON item_bonuses (equipment_slot);
CREATE INDEX IF NOT EXISTS item_bonuses_page_idx ON item_bonuses (lower(page_name));

-- ---------------------------------------------------------------------------
-- Drop tables.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS item_drops (
    id             BIGSERIAL PRIMARY KEY,
    item_name      TEXT NOT NULL,
    item_id        INTEGER,
    source_name    TEXT NOT NULL,          -- 'Abyssal demon'
    source_version TEXT,                   -- 'Standard', from the part after '#'
    -- Both the wiki's own text and the parsed probability. The text is kept because several
    -- hundred rows say 'Varies', 'Unknown' or 'Rare', which have no numeric meaning: rarity is
    -- null for those rather than guessed, and a caller can still show what the wiki said.
    rarity_text    TEXT,
    rarity         NUMERIC,
    quantity_low   INTEGER,
    quantity_high  INTEGER,
    rolls          INTEGER,
    drop_type      TEXT,                   -- 'combat' | 'thieving' | 'reward' | ...
    rare_drop_table BOOLEAN NOT NULL DEFAULT false,
    synced_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS item_drops_source_idx ON item_drops (lower(source_name));
CREATE INDEX IF NOT EXISTS item_drops_item_idx ON item_drops (item_id);

-- ---------------------------------------------------------------------------
-- Monsters. Combat level and hitpoints are what turn a drop table into a GP/hr figure.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS monsters (
    id                BIGSERIAL PRIMARY KEY,
    page_name         TEXT NOT NULL,
    version_anchor    TEXT,
    name              TEXT,
    combat_level      INTEGER,
    hitpoints         INTEGER,
    slayer_level      INTEGER,
    slayer_experience DOUBLE PRECISION,
    members           BOOLEAN NOT NULL DEFAULT false,
    synced_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS monsters_name_idx ON monsters (lower(name));
CREATE INDEX IF NOT EXISTS monsters_page_idx ON monsters (lower(page_name));
