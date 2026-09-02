# Gielinomics

A time-series service that retains OSRS market history the official APIs don't, with a query API and alerting layer on top.

Gielinor plus economics: the long-run price record for a world that only ever publishes the last few hours of it.

> **Status: all seven phases implemented, plus account tracking.** Ingest, gap repair, the query
> API, alerting, hiscore polling, the frontend and the wiki Bucket sync all run.
> See [`plan.md`](plan.md) for the full design.

**The moat is the dataset.** The upstream APIs serve recent windows only, and `/timeseries` at a one-year lookback returns *daily* bars — there is no fine-grained backfill. The 5-minute series can only ever start the day ingest does, which is why Phase 1 is "turn the worker on and leave it running".

## Layout

```
src/
├── Gielinomics.Client/    # NuGet: Gielinomics.Osrs.Client. References nothing else here.
├── Gielinomics.Data/      # Npgsql + Dapper repositories
├── Gielinomics.Ingest/    # BackgroundServices: poll, gap-repair, backfill, staleness
├── Gielinomics.Api/       # ASP.NET Core minimal API
└── Gielinomics.Alerts/    # Rule evaluation, GE tax, webhook validation
tests/
├── Gielinomics.Client.Tests/   # recorded fixtures, no network in CI
└── Gielinomics.Ingest.Tests/   # scheduling arithmetic, failure classification
db/init/                 # schema, applied on first container start
web/                     # Vite + React + TS, API types generated from the OpenAPI doc
```

`Gielinomics.Client` must not reference any other project in this solution. It's a standalone package that happens to have this repo as its first consumer.

## Running

```bash
cp .env.example .env      # fill in POSTGRES_PASSWORD and GIELINOMICS_USER_AGENT
docker compose up -d postgres
dotnet build
dotnet test
```

`db/init/*.sql` is applied by the container on first start only. If the schema changes,
`docker compose down -v` to drop the volume and let it re-apply — or, on a database you do not
want to drop, apply the new file by hand:

```bash
docker compose exec -T postgres psql -U gielinomics -d gielinomics < db/init/02_wiki.sql
```

Then bring up the rest:

```bash
docker compose up -d          # postgres, ingest, api, grafana
curl localhost:8080/api/ingest/status    # per-feed health
curl localhost:9091/metrics              # ingest Prometheus metrics

cd web && npm install && npm run dev      # frontend on :5173, proxying /api to :8080
```

The worker starts polling immediately and repairs backwards over its repair window on boot,
so a fresh database has several hours of 5-minute history within a minute or two of starting.

### Endpoints

| Route | Notes |
|---|---|
| `GET /api/items` | Search by name, members, buy limit. Keyset-paginated via `cursor`. |
| `GET /api/items/{id}` | ETag + conditional `304`. |
| `GET /api/items/{id}/prices` | `from`, `to`, `interval` of `5m`, `1h` or `1d`. |
| `GET /api/items/{id}/stats` | Volatility, mean spread, liquidity over `window`. |
| `GET /api/market/movers` | Ranked by percentage change over `window`. |
| `GET /api/market/spreads` | Margin scan, **tax-adjusted**, ranked by profit per buy limit. |
| `GET /api/ingest/status` | Per-feed last success and recent failures. |
| `GET /api/ingest/coverage` | Fraction of expected windows actually retained. |
| `GET /api/gear` | Equipment ranked by a stat **against its price** — gp per point. |
| `GET /api/monsters/{name}/drops` | A drop table priced against retained history: gp per kill. |
| `GET /api/items/{id}/bonuses` | Equipment stats. |
| `GET /api/items/{id}/drops` | Everything known to drop this item. |
| `GET /api/players/{name}` | Resolves by **any** name the account has used. |
| `GET /api/players/{name}/history` | Per-skill samples, with the mapping the indices decode under. |
| `GET /api/players/{name}/gains` | XP and levels over `day`, `week`, `month` or `year`. |
| `POST /api/players/{name}/track` | **Authenticated.** Detects account type, then starts polling. |
| `GET /api/alerts` | **Authenticated.** |
| `POST /api/alerts` | **Authenticated.** Webhook host allowlisted on write. |

The OpenAPI document is at `/openapi/v1.json` — that is what the Phase 6 frontend generates
its types from.

### Issuing an API token

The two write routes are the only mutable surface. Tokens are stored as SHA-256 hashes, so
there is nothing in the table to steal:

```bash
TOKEN=$(openssl rand -hex 32)
HASH=$(printf '%s' "$TOKEN" | openssl dgst -sha256 -binary | xxd -p -c 64)
docker compose exec -T postgres psql -U gielinomics -d gielinomics \
  -c "INSERT INTO api_users (label, token_hash) VALUES ('my-token', decode('$HASH', 'hex'));"
echo "$TOKEN"    # shown once; the database only ever sees the hash
```

Then `curl -H "Authorization: Bearer $TOKEN" localhost:8080/api/alerts`.

### One-shot backfill

`/timeseries?lookback=1y` yields 365 **daily** bars per item — long-horizon context, not 5m
history. Roughly 4,700 items at 1 req/s is about 80 minutes. Run it once, overnight:

```bash
GIELINOMICS_RUN_BACKFILL=true docker compose up -d ingest
# ...then set it back to false and restart, or it re-runs on every boot.
```

### Running the API or ingest worker outside Docker

Compose injects `ConnectionStrings__Gielinomics` as an environment variable; a bare
`dotnet run` or an IDE launch gets nothing, and `appsettings.json` holds an empty
placeholder. Put the value in user secrets once per project:

```bash
# Host port is 5433 -- 5432 is left to any native Postgres install.
CS="Host=localhost;Port=5433;Database=gielinomics;Username=gielinomics;Password=$POSTGRES_PASSWORD"
dotnet user-secrets set "ConnectionStrings:Gielinomics" "$CS" --project src/Gielinomics.Api
dotnet user-secrets set "ConnectionStrings:Gielinomics" "$CS" --project src/Gielinomics.Ingest
dotnet user-secrets set "Gielinomics:UserAgent" "$GIELINOMICS_USER_AGENT" --project src/Gielinomics.Ingest
```

User secrets load **only** in the Development environment. The `launchSettings.json`
profiles set it; if you run the built binary directly, export `ASPNETCORE_ENVIRONMENT=Development`
(`DOTNET_ENVIRONMENT` for the ingest worker) or the host falls back to the empty
placeholder and throws at startup.

`GIELINOMICS_USER_AGENT` is **required**, not advisory. The wiki pre-emptively blocks a list of default agents — including `RestSharp`, `python-requests` and `curl` — so the client throws at construction rather than letting you discover it as a 403 storm in production. Use something like `gielinomics/0.1 (github.com/N0tT1m/gielinomics)`.

## What makes the history trustworthy

These could not have been added retroactively, so they shipped with the first working worker:

- **`ingest_runs` on every poll attempt**, successful or not — the only thing distinguishing a
  quiet market from a dead worker. Readable at `GET /api/ingest/status`.
- **Gap detection and backwards repair** via the `timestamp` parameter. Every worker sweeps its
  repair window on boot, after any failure, and every twelfth poll. `GET /api/ingest/coverage`
  reports the fraction of expected windows actually present.
- **A staleness alarm** at 15 minutes for `5m`, 5 minutes for `latest`, 3 hours for `1h`. Logged
  at `Error` and published as the `gielinomics.ingest.staleness` gauge, so Grafana can alert on
  it without the Phase 5 alerting layer being involved.

The `/latest` poll is change-gated on trade timestamps: writing every response wholesale would
be ~5.3M near-identical rows a day. In practice a 60-second poll writes a handful of rows.

## Account tracking

An allowlist, never a crawl: nothing is polled that somebody did not `POST .../track`. Each
tracked account is revisited hourly, with requests spread across the interval rather than fired
in a burst, and a separate rate limit budget from the wiki's — Jagex publishes no rate limit,
which is not the same as not having one.

Three things about the hiscores are worth knowing before changing any of this, all verified
against the live API:

- **`index_lite.json` names every field** (`id` *and* `name` per entry). The plan was written
  around the positional CSV, where the ordering is the entire contract and one inserted boss
  misattributes every field after it. The JSON removes that hazard, so it is the primary path;
  `HiscoreCsvParser` remains as the documented fallback, and `HiscoreMapping` is versioned and
  stamped onto every snapshot either way.
- **A 404 is a result, not an error.** It means "not on this table", which is what makes
  account type inferable at all — a main gets a 404 from the ironman table. It also means a
  hardcore death or a rename, so the worker logs it rather than retrying.
- **Rank is excluded from the snapshot dedup hash.** A dormant account's rank moves constantly
  as other players pass it. Hashing it would mean the dedup never fires and
  `hiscore_snapshots` grows by a row per account per hour forever. The hash covers what the
  player *did* — levels, XP, activity scores. Rank is still stored and still queryable.

Renames resolve through `player_names`, so an account looked up by an old name returns the same
timeline rather than a 404 or a second, empty history.

## Frontend

`web/` is a Vite + React + TS app whose API types are **generated** from `/openapi/v1.json`
(`npm run generate:api`), not hand-written. That is why every endpoint carries a
`.Produces<T>()` annotation: an endpoint returning an anonymous object generates a client that
types the response as `unknown`, which is worse than not generating one, because it looks like
it worked.

Charts are hand-rolled SVG on the validated data-viz palette — see [`web/README.md`](web/README.md)
for the rules they hold to. The one worth repeating here: **a window with no trades breaks the
line rather than interpolating across it.** Drawing a confident straight line through data this
platform does not have is exactly the claim `ingest_runs` exists to stop anyone making.

## Wiki structured data

The weekly Bucket sync is what makes the retained prices worth more than the price API alone.
The wiki knows what drops and how often but nothing about what it costs; the price API knows
what it costs but nothing about drops. Multiplying them is the product:

```bash
curl "localhost:8080/api/monsters/Abyssal%20demon/drops"   # ~9,000 gp per kill
curl "localhost:8080/api/gear?stat=strength&slot=2h&cheapestFirst=true"
```

Weird Gloop replaced Semantic MediaWiki with their own **Bucket** extension — `action=ask` is
hard-deprecated, so anything written about querying this wiki with SMW or Cargo is out of date.
Four things about it were worth discovering before writing any code, and three of them changed
the design:

- **Booleans are not booleans.** A set flag arrives as an empty string and an unset one is
  omitted from the row entirely. Modelling them as `bool?` is the obvious move and fails to
  parse every row that has one.
- **`orderBy` requires the field to be selected**, and offset paging without an order silently
  repeats and skips rows. `BucketQuery` adds the ordering field to the projection for you.
- **A bad query returns HTTP 200** with an `error` field. A status check alone reads a Lua
  syntax error as a successful empty sync — which, for a full-replace load, would empty the table.
- **Rarity is written for people.** Mostly `1/512`, but also `1/5,461.33` with a separator and a
  decimal, and several hundred rows of `Varies`, `Unknown`, `Rare`. Those parse to **null**, not
  to a guess: an invented probability becomes a gp-per-kill figure somebody acts on. They are
  counted and reported so a kill's value is stated as a floor rather than a total.

Each sync replaces a table wholesale in one transaction — Bucket exposes no row identity to
upsert against — using `DELETE` rather than `TRUNCATE`, so readers keep seeing the previous
contents until it commits rather than stalling on an exclusive lock.

## Still to build

- **Wise Old Man** — `plan.md` recommends consuming it for group and competition features
  rather than reimplementing them. Nothing here touches it yet.

## Before writing any margin logic

GE tax is 2% (raised from 1% on 29 May 2025), capped at 5,000,000 gp per item, with no tax under 50 gp and a real exempt list. It lives in `GrandExchangeTaxRules` as data because it has already moved once. A margin scan that ignores it is wrong, and obviously so to any player.

## Attribution and licence

Price data comes from the [OSRS Wiki real-time prices API](https://oldschool.runescape.wiki/w/RuneScape:Real-time_Prices) and is licensed **CC BY-NC-SA 3.0**. Attribute the wiki. Do not build a commercial product on it. Before running a 24/7 pipeline, say hello in `#api-discussion` on the wiki Discord.

Code in this repo is MIT.
