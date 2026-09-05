# OSRS Data Platform — Plan

**Pitch:** A time-series service that retains OSRS market and account history the official APIs don't, with a query API and alerting layer on top.

**Stack:** .NET 9 / C#, PostgreSQL + TimescaleDB, Docker Compose on `goose`, thin React/TS frontend.

**The moat is the dataset.** The upstream APIs serve recent windows only. Nobody retains long-horizon history in a queryable form. Start the ingest worker on day one and let it accumulate while the rest gets built.

---

## Components

| Component | Project | Purpose |
|---|---|---|
| Client library | `Osrs.Client` | Typed async wrappers over all upstream sources. Published to NuGet. |
| Ingest worker | `Osrs.Ingest` | `BackgroundService`. Polls, normalises, upserts. |
| Data layer | `Osrs.Data` | EF Core entities + migrations, or Dapper + raw SQL. |
| Query API | `Osrs.Api` | ASP.NET Core minimal API over accumulated history. |
| Alerting | `Osrs.Alerts` | Rule evaluation → Discord webhooks. |
| Frontend | `web/` | Vite + React + TS. Charts, watchlists, account timelines. |

```
osrs-platform/
├── src/
│   ├── Osrs.Client/          # NuGet package — no dependencies on anything else here
│   ├── Osrs.Data/
│   ├── Osrs.Ingest/
│   ├── Osrs.Api/
│   └── Osrs.Alerts/
├── tests/
│   ├── Osrs.Client.Tests/    # recorded HTTP fixtures, no live calls in CI
│   └── Osrs.Api.Tests/
├── web/
├── docker-compose.yml
└── plan.md
```

`Osrs.Client` must not reference any other project in the solution. It's a standalone package that happens to have this repo as its first consumer.

---

## Non-goals

- No game client interaction of any kind. External data only.
- No account credentials, no automation, no ToS-adjacent behaviour.
- Not rebuilding Wise Old Man. Consume it where it already solves the problem.

### Scope decision: account tracking

The non-goal above and the `players` / `hiscore_snapshots` / `skill_samples` / `/gains` design are in direct conflict — that *is* Wise Old Man. Worth resolving before Phase 2 rather than discovering it halfway through.

The moat argument does not apply here. It applies to prices, where nobody retains long-horizon history; WOM's snapshot history predates anything this project could start collecting by years. Account tracking also carries essentially all of the fragility in this document: the positional CSV contract, the schema-drift alarm, account-type inference across ten tables, the rate-limit relationship with Jagex (who are less forgiving than the wiki), and the largest row-count on disk.

**Recommended:** cut hiscore polling from v1 and read WOM for anything account-shaped. Nothing is given up that this project has an advantage in.

**If kept anyway:** `mapping_version` on every snapshot is non-negotiable, and the tracked set stays an allowlist.

---

## Data sources

### 1. Wiki Real-time Prices (Grand Exchange)

Base: `https://prices.runescape.wiki/api/v2/osrs`

**Note:** this is **v2**. Older wrappers and blog posts reference `v1` — that's the main reason existing C# packages are stale.

| Route | Params | Returns |
|---|---|---|
| `/latest` | `id` (optional) | Map of itemId → `{high, highTime, low, lowTime}` |
| `/mapping` | — | Array of `{id, name, examine, members, lowalch, highalch, limit, value, icon}` |
| `/5m` | `timestamp` (optional) | Map of itemId → `{avgHighPrice, highPriceVolume, avgLowPrice, lowPriceVolume}` |
| `/1h` | `timestamp` (optional) | Same shape as `/5m` |
| `/timeseries` | `id` (**required**), `lookback` (**required**) | `{data[], itemId, startTimestamp, endTimestamp, timestep}` |

`lookback` valid values: `6h`, `24h`, `7d`, `30d`, `6m`, `1y`.

**Granularity is tied to the lookback window, and this is the single most consequential fact in this document.** Verified against the live endpoint: `lookback=1y` returns **365 points at `timestep: 86400`** — daily bars, not 5-minute ones. Fine-grained history is only available over the short windows.

The implications:

- **There is no 5m backfill.** Your 5-minute series begins the hour the worker starts and can never be extended backwards. This is why ingest starts on day one — it is the only mechanism that ever produces that data.
- **Backfill and live ingest write at different granularities.** Daily bars cannot share a key space with 5-minute ones. See `price_series` in the schema, where `step_seconds` is part of the primary key.
- The top-level field is **`timestep`**, not `step`. Read it from the response; don't assume it from the lookback you asked for.

**Gotchas that will bite:**

- **Prices can exceed `Int32`.** Use `long`. Average prices may carry decimals — `decimal` or `double` on the `/5m` and `/1h` routes.
- **Nulls are meaningful.** `high`/`highTime` are null when an item has never had a recorded instant-buy. Model as nullable; don't coalesce to zero.
- **Never loop `/latest?id=` over every item.** The wiki explicitly calls this out. One bulk call, ~3700 items.
- **`/timeseries` granularity is not guaranteed** and can change without warning. Read `timestep` from the response and store it alongside the rows rather than assuming it from the `lookback` you requested.
- **User-Agent is mandatory in practice.** The wiki pre-emptively blocks a list of default agents — including **`RestSharp`**, which is directly relevant here. Also blocked: `python-requests`, `Python-urllib`, `Apache-HttpClient`, `Java/{version}`, `curl/{version}`. Set something like `osrs-platform/0.1 (github.com/<you>/osrs-platform)`.
- No published rate limit, but sustained multiple-large-queries-per-second gets you cut off. They ask heavy users to say hello in `#api-discussion` on the wiki Discord — worth doing before running a 24/7 pipeline.
- Data is CC BY-NC-SA 3.0. Attribute the wiki; don't monetise.

### 2. Official Hiscores

Base: `https://secure.runescape.com/m={table}/index_lite.ws?player={name}`

A `.json` variant (`index_lite.json`) exists and returns structured skills/activities arrays — verify the current shape yourself before committing to it, and keep the CSV parser as a fallback.

| Account type | `m=` table |
|---|---|
| Main | `hiscore_oldschool` |
| Ironman | `hiscore_oldschool_ironman` |
| Hardcore ironman | `hiscore_oldschool_hardcore_ironman` |
| Ultimate ironman | `hiscore_oldschool_ultimate` |
| Deadman | `hiscore_oldschool_deadman` |
| Seasonal / Leagues | `hiscore_oldschool_seasonal` |
| Tournament | `hiscore_oldschool_tournament` |
| Skiller | `hiscore_oldschool_skiller` |
| Skiller (1 def) | `hiscore_oldschool_skiller_defence` |
| Fresh Start Worlds | `hiscore_oldschool_fresh_start` |

**CSV format:** newline-delimited, three comma-separated values per line. Skills are `rank,level,xp`; activities are `rank,score`. `-1` means unranked. Order is positional and undocumented — **the ordering is the entire contract**, and Jagex appends new bosses/activities on game updates without notice.

Mitigation: keep the index→name mapping in a versioned config file, not hardcoded. Log and alert when a response has more lines than the mapping knows about, rather than silently misattributing every field after the insertion point.

**Gotchas:**

- **No CORS headers.** Browsers cannot call this directly. This is the concrete justification for your API existing — the frontend proxies through it.
- 404 for a nonexistent or unranked player. Distinguish that from a transient failure.
- Account type is not returned. Infer it by querying multiple tables and comparing, the way `osrs-json-hiscores` does — hardcore deaths and de-ironing show up as divergence between tables.
- No documented rate limit, but Jagex is less forgiving than the wiki. Cap at ~1 req/sec and cache aggressively.
- Names are case-insensitive, allow spaces (URL-encode), and can change. Track accounts by a stable internal ID.

### 3. Wiki structured data — Bucket

Base: `https://oldschool.runescape.wiki/api.php?action=bucket&format=json&query={lua}`

Weird Gloop replaced Semantic MediaWiki with their own **Bucket** extension. `action=ask` is hard-deprecated and slated for removal — anything you read referencing SMW or Cargo for this wiki is out of date.

Queries are Lua strings passed as a query parameter:

```
bucket('infobox_item')
  .select('item_id','image','examine')
  .where('item_name','Raw lobster')
  .run()
```

Buckets are SQL-like tables — `infobox_item`, `exchange`, `storeline`, and others. Enumerate what's actually available on `RuneScape:Bucket` before designing your schema around assumed table names.

Fallbacks for anything Bucket doesn't expose:
- `action=parse&prop=wikitext&page={title}` — raw wikitext, then parse infobox templates yourself. Brittle, but it's how drop tables were historically extracted.
- `action=query&list=categorymembers` — enumerate pages by category.

This is a **weekly** sync at most. It's near-static reference data.

### 4. Wise Old Man

Base: `https://api.wiseoldman.net/v2`

Useful routes: `/players/{username}`, `/players/{username}/gained`, `/players/{username}/snapshots`, `/groups/{id}`, `/groups/{id}/gained`, `/competitions/{id}`, `/efficiency/rates`.

Descriptive User-Agent required. Register for an API key (`x-api-key` header) for higher limits. Check their current documented rate limits before building against it.

Consume this for group/competition features rather than reimplementing. Their snapshot history predates yours by years.

---

## Schema

Postgres 16 + TimescaleDB. Hypertables on `price_series`, `price_latest`, and `skill_samples`.

```sql
-- Reference data, refreshed from /mapping
CREATE TABLE items (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    examine       TEXT,
    members       BOOLEAN NOT NULL,
    buy_limit     INTEGER,          -- nullable: not all items have one
    value         BIGINT,
    lowalch       BIGINT,
    highalch      BIGINT,
    icon          TEXT,
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT now()
);

**No foreign keys from the hypertables to `items`.** New item IDs appear in `/5m` the moment they become tradeable — up to 24h before the daily `/mapping` sync knows about them — and one unknown ID would fail the entire batch insert. Upsert a stub `items` row on an unseen ID instead, and let the next mapping sync fill in the details. Per-row FK checks are a bulk-insert tax on a hypertable regardless.

```sql
-- Aggregated price bars at any granularity. step_seconds is part of the key
-- so live 5m ingest and daily /timeseries backfill coexist without collision.
CREATE TABLE price_series (
    item_id       INTEGER NOT NULL,
    step_seconds  INTEGER NOT NULL,       -- 300 live, 86400 from lookback=1y
    bucket_ts     TIMESTAMPTZ NOT NULL,   -- start of the window
    avg_high      NUMERIC,
    avg_low       NUMERIC,
    high_volume   BIGINT,
    low_volume    BIGINT,
    source        TEXT NOT NULL,          -- '5m' | '1h' | 'timeseries'
    PRIMARY KEY (item_id, step_seconds, bucket_ts)
);
SELECT create_hypertable('price_series', 'bucket_ts');
```

`/1h` polling lands here with `step_seconds = 3600`, which gives gap repair somewhere to write. Rollups beyond 90 days become additional `step_seconds` values produced by continuous aggregates rather than separate tables.

```sql
-- Latest observed trades, for spread/liquidity work.
-- APPEND ONLY WHEN THE TRADE TIMESTAMPS CHANGE. /latest returns the same
-- high/low until a trade actually occurs; illiquid items would otherwise
-- produce thousands of byte-identical rows per day.
CREATE TABLE price_latest (
    item_id      INTEGER NOT NULL,
    observed_at  TIMESTAMPTZ NOT NULL,
    high         BIGINT,
    high_time    TIMESTAMPTZ,
    low          BIGINT,
    low_time     TIMESTAMPTZ,
    PRIMARY KEY (item_id, observed_at)
);
SELECT create_hypertable('price_latest', 'observed_at');
```

The ingest worker holds the last `(high_time, low_time)` per item in memory and writes only on change. Naive 60s writes would be ~5.3M rows/day — five times the volume of the 5m series — and almost entirely duplicates.

```sql
-- Every attempted poll, whether or not it produced rows.
-- Without this there is no way to distinguish "the market was quiet"
-- from "the worker was down", and no way to reconstruct it later.
CREATE TABLE ingest_runs (
    id            BIGSERIAL PRIMARY KEY,
    source        TEXT NOT NULL,          -- '5m' | 'latest' | 'mapping' | ...
    target_bucket TIMESTAMPTZ,            -- the window being fetched, if any
    attempted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome       TEXT NOT NULL,          -- 'ok' | 'http_error' | 'parse_error' | ...
    rows_written  INTEGER,
    detail        TEXT
);
CREATE INDEX ON ingest_runs (source, target_bucket);

CREATE TABLE players (
    id              BIGSERIAL PRIMARY KEY,
    display_name    TEXT NOT NULL,
    normalised_name TEXT NOT NULL UNIQUE,   -- lowercased, spaces normalised
    account_type    TEXT NOT NULL,
    tracked         BOOLEAN NOT NULL DEFAULT true,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Names change, and a rename must not split one player's history in two.
-- All name-keyed API lookups resolve through here to a stable player_id.
CREATE TABLE player_names (
    player_id  BIGINT NOT NULL REFERENCES players(id),
    name       TEXT NOT NULL,
    normalised TEXT NOT NULL,
    seen_from  TIMESTAMPTZ NOT NULL,
    seen_to    TIMESTAMPTZ,            -- null = current
    PRIMARY KEY (player_id, normalised, seen_from)
);
CREATE INDEX ON player_names (normalised);

CREATE TABLE hiscore_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    player_id       BIGINT NOT NULL REFERENCES players(id),
    captured_at     TIMESTAMPTZ NOT NULL,   -- first capture with this content
    last_seen_at    TIMESTAMPTZ NOT NULL,   -- most recent capture, bumped on match
    payload         JSONB NOT NULL,         -- raw parsed response
    content_hash    BYTEA NOT NULL,
    mapping_version INTEGER NOT NULL,       -- which index→name mapping decoded this
    UNIQUE (player_id, content_hash)
);

-- Flattened for querying; derived from snapshots
CREATE TABLE skill_samples (
    player_id   BIGINT NOT NULL REFERENCES players(id),
    captured_at TIMESTAMPTZ NOT NULL,
    skill       SMALLINT NOT NULL,   -- positional index, decoded via mapping_version
    rank        INTEGER,
    level       SMALLINT,
    xp          BIGINT,
    PRIMARY KEY (player_id, captured_at, skill)
);
SELECT create_hypertable('skill_samples', 'captured_at');

CREATE TABLE alert_rules (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL REFERENCES api_users(id),
    kind        TEXT NOT NULL,      -- 'margin' | 'volume' | 'xp_milestone'
    config      JSONB NOT NULL,
    webhook_url TEXT NOT NULL,      -- validated: discord.com / discordapp.com only
    enabled     BOOLEAN NOT NULL DEFAULT true,
    last_fired  TIMESTAMPTZ
);
```

**Snapshot dedup.** `UNIQUE (player_id, content_hash)` is the constraint that actually does the work. Including `captured_at` would have made every row unique by construction, so the "skip write when unchanged" intent would never have fired. Unchanged captures bump `last_seen_at` via `ON CONFLICT DO UPDATE`; only genuinely new content inserts.

**Mapping version.** `hiscore_snapshots.mapping_version` records which index→name mapping decoded that payload. When Jagex appends a boss mid-game-update, old rows remain correctly interpretable instead of silently shifting by one field.

Retention: TimescaleDB continuous aggregates to roll the `step_seconds = 300` rows into 3600 and 86400 beyond ~90 days. Keep the raw 5m data as long as disk allows — it's the asset. **Refresh policies must cover the backfill window**, or late-arriving `/timeseries` history will be silently excluded from every rollup.

Storage estimate:

| Table | Rows/day | Note |
|---|---|---|
| `price_series` (5m) | ~1.07M | 3700 items × 288 |
| `price_latest` | ~50–200k | change-gated; would be 5.3M if written naively every 60s |
| `skill_samples` | ~720k per 1000 tracked players | 24 captures × ~30 rows |

Compressed, prices are manageable indefinitely on goose. Player tracking is what actually threatens the disk budget, which is one more argument for the scope decision below.

---

## Ingest design

| Job | Cadence | Source | Destination |
|---|---|---|---|
| Item mapping sync | Daily | `/mapping` | `items` |
| 5m price poll | Every 5 min, offset ~30s past the boundary | `/5m` | `price_series` @ 300 |
| 1h price poll | Hourly | `/1h` | `price_series` @ 3600 |
| Latest price poll | Every 60s | `/latest` | `price_latest`, change-gated |
| Daily backfill | One-shot | `/timeseries?lookback=1y` | `price_series` @ 86400 |
| Hiscore poll | Hourly per tracked player, staggered | `index_lite` | `hiscore_snapshots` |
| Wiki Bucket sync | Weekly | `action=bucket` | reference tables |

**Requirements:**

- **Idempotent writes.** `ON CONFLICT DO UPDATE`. The 5m poll will re-fetch overlapping windows; that must be a no-op.
- **Gap detection and repair — in Phase 1, not later.** Track the last successfully persisted bucket per source. On startup and after any failure, walk backwards using the `timestamp` parameter on `/5m` and `/1h` to fill holes. This is the single most important correctness property, which is exactly why it cannot wait for Phase 2 — weeks of unaudited Phase 1 ingest produces precisely the untrustworthy dataset this is meant to prevent.
- **Write `ingest_runs` on every attempt, from the first commit.** It costs one insert per poll and it is the only thing that distinguishes a quiet market from a dead worker. It cannot be reconstructed after the fact.
- **Staleness alarm on day one.** Silent death is the realistic failure mode of a 24/7 poller. Alert when no new bucket has landed in 15 minutes. This belongs in Phase 1 alongside the worker, not in the Phase 5 alerting work.
- **Backfill on bootstrap.** `/timeseries?lookback=1y` per item yields 365 **daily** bars — it seeds long-horizon context, not 5m history. ~3700 requests at 1/sec is about an hour, so run it for every item once, overnight. There is no reason to curate a subset.
- **Rate limiting.** `System.Threading.RateLimiting`, one limiter per upstream host. Wiki and Jagex get different budgets.
- **Retries.** Polly via `IHttpClientFactory` — exponential backoff with jitter, circuit breaker per host. Do not retry 404s.
- **Schema drift alarm.** Alert when the hiscores response line count exceeds the known mapping, or when `/mapping` returns fields you don't recognise.
- **`ActivitySource` + OpenTelemetry** from the start. Poll latency, rows written, gap counts.

---

## Query API

The value is the questions the upstream APIs can't answer.

```
GET  /api/items                                  # search, filter by members/limit
GET  /api/items/{id}
GET  /api/items/{id}/prices?from=&to=&interval=  # your retained history
GET  /api/items/{id}/stats                       # volatility, mean spread, liquidity
GET  /api/market/movers?window=24h               # ranked by % change
GET  /api/market/spreads?minVolume=              # margin scan, tax-adjusted
GET  /api/players/{name}
GET  /api/players/{name}/history?skill=&from=
GET  /api/players/{name}/gains?period=week
POST /api/players/{name}/track
GET  /api/alerts
POST /api/alerts
```

Notes:

- **The two `POST` routes are authenticated writes.** They are currently the only mutable surface and both are abusable: `/track` adds unbounded polling load, and `/alerts` accepts a URL that your server will then make outbound requests to — an open request relay pointed at your own network. Requires an `api_users` table and a token check before either route is reachable from anything but localhost.
- **Validate `webhook_url` against a host allowlist** (`discord.com`, `discordapp.com`) at write time, not at fire time. Reject everything else outright.
- **Name-keyed player routes resolve through `player_names`**, so a rename doesn't 404 or split a timeline.
- **GE tax matters.** Any margin calculation that ignores it is wrong, and obviously so to any player. Current rules, verified:
  - **2%** on sales, raised from 1% on **29 May 2025**
  - Capped at **5,000,000 gp per item**
  - **No tax on items priced under 50 gp**
  - Exempt: bonds, energy potion, bronze/iron/steel arrows and darts, mind rune, most basic foods (bass, bread, cake, chicken, herring, lobster, mackerel, pike, salmon, shrimps, tuna, meat pie), the common teleport tablets, games necklace(8), ring of dueling(8), and basic tools (chisel, hammer, needle, rake, saw, secateurs, spade)

  Encode this as **data, not a switch statement** — the rate has already moved once, and the exempt list moves with game updates.
- Output cursor-paginated, `Cache-Control` on hot reads, ETag on item metadata.
- OpenAPI document generated; that's what the frontend types are generated from.

---

## Client library — design bar

This is the artifact people will read. Treat it as the portfolio centrepiece.

- Async-only, `CancellationToken` on every method.
- `IHttpClientFactory` integration, `AddOsrsClient()` DI extension.
- Source-generated `System.Text.Json` contexts — no reflection-based serialisation.
- Nullable reference types on, warnings as errors.
- `IAsyncEnumerable<T>` where results page.
- User-Agent required at construction time. Throw if unset — the API will block you anyway, so fail early and loudly.
- XML docs on every public member, `<GenerateDocumentationFile>`.
- SemVer, `CHANGELOG.md`, GitHub Actions publishing on tag.
- Tests against recorded fixtures. No network in CI.

---

## Phases

**Phase 1 — Ingest (start immediately).** Prices only. `/mapping` + `/5m` + `/latest` into Postgres. Docker Compose on goose. No API, no frontend. Get data accumulating.

Non-negotiable in Phase 1, because none of it can be added retroactively:

- `ingest_runs` written on every poll attempt
- gap detection and backwards repair via the `timestamp` parameter
- staleness alarm at 15 minutes with no new bucket

Everything else in Phase 2 is deferrable. These three are what make the resulting dataset trustworthy, and a dataset nobody trusts is not a moat.

**Phase 2 — Backfill and hardening.** `/timeseries` daily backfill, retries and circuit breakers, OpenTelemetry metrics, continuous aggregates with refresh policies covering the backfill window. Hiscore polling only if the scope decision above lands on keeping it.

**Phase 3 — Extract the library.** Pull the HTTP layer out of the worker into `Osrs.Client`. The worker becomes the first consumer. Publish `0.1.0` to NuGet.

**Phase 4 — Query API.** ASP.NET Core over the accumulated data. By now you have weeks of history, so the endpoints return something real.

**Phase 5 — Alerting.** Rule evaluation, Discord webhooks. First actual users.

**Phase 6 — Frontend.** React/TS against the OpenAPI spec. Charts, watchlists, timelines.

**Phase 7 — Wiki Bucket.** Drop tables and item stats. Unlocks GP/hr and gear comparison — the cross-source joins that justify the whole design.

---

## Operational

- Compose stack on goose alongside the existing services: `postgres` (TimescaleDB image), `ingest`, `api`, `grafana`.
- Bind the API to `0.0.0.0` for development from the Windows box, same as the LocalStack setup.
- Named volume for Postgres data, with a `pg_dump` cron to `/mnt/ai/backups/`. The dataset is the moat — losing it resets the project. **Built:** `ops/backup.sh`, cron line in `ops/README.md`.
- **`pg_dump` degrades badly as hypertables grow.** Fine at first; revisit physical backups (`pg_basebackup` / WAL archiving) once the 5m table is measured in tens of GB. The size query to watch is in `ops/README.md`.
- **Test the restore once, before you need it.** An untested backup of the one irreplaceable asset is not a backup. **Built:** `ops/verify-restore.sh` restores the newest dump into a throwaway database and asserts on row counts, weekly from cron. Restoring into an empty database is not the failure mode to guard against — a TimescaleDB restore missing its pre/post hooks reports success and leaves every hypertable empty, which is why the checks are on `count(*)` and not on exit codes.
- **Get a copy off goose.** The moat currently lives on a single disk in a single box; a drive failure resets the project by however many months of accumulation it holds. `ops/backup.sh` rsyncs to `GIELINOMICS_BACKUP_REMOTE` when it is set, and warns on stderr when it is not. **Still needs a destination chosen.**
- Grafana over Postgres directly for internal dashboards; the React app is for the public-facing view.

---

## Verify before building

Things that could have moved and are worth ten minutes each:

- [ ] `index_lite.json` response shape and whether all account tables support it
- [ ] Current bucket names and fields on `RuneScape:Bucket`
- [ ] Wise Old Man v2 rate limits and whether an API key is required
- [ ] Existing NuGet packages' last-publish dates and download counts, to confirm the gap is still open

Resolved:

- [x] **Prices API is `v2`,** and `/timeseries` takes `lookback`, not `timestep`. Confirmed live. (`v1` still responds, so don't take a working v1 call as evidence it's current.)
- [x] **`lookback=1y` returns 365 daily bars** at `timestep: 86400`. No 5m backfill exists. Confirmed live against item 4151.
- [x] **GE tax:** 2% since 29 May 2025, 5M gp cap per item, no tax under 50 gp, plus the exempt list in the Query API section.

---

## Open questions

- **Account tracking at all** — see the scope decision under Non-goals. This is the one that changes the shape of the project; decide it before Phase 2.
- **Tracked accounts.** If kept: self-serve registration invites unbounded polling load. Start with an allowlist.
- **Public or private.** Public hosting means a domain, ToS, and the wiki's non-commercial licence to respect. Local-only is a perfectly good v1 and defers all of it.
