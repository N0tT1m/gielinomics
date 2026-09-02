# Gielinomics

A time-series service that retains OSRS market history the official APIs don't, with a query API and alerting layer on top.

Gielinor plus economics: the long-run price record for a world that only ever publishes the last few hours of it.

> **Status: Phases 1-5 implemented.** Ingest, gap repair, the query API and alerting all run.
> Not built: account tracking (`plan.md`'s scope decision recommends cutting it in favour of
> Wise Old Man), the Phase 7 wiki Bucket sync, and the Phase 6 frontend in `web/`.
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
web/                     # Phase 6: Vite + React + TS
```

`Gielinomics.Client` must not reference any other project in this solution. It's a standalone package that happens to have this repo as its first consumer.

## Running

```bash
cp .env.example .env      # fill in POSTGRES_PASSWORD and GIELINOMICS_USER_AGENT
docker compose up -d postgres
dotnet build
dotnet test
```

`db/init/01_schema.sql` is applied by the container on first start only. If the schema
changes, `docker compose down -v` to drop the volume and let it re-apply.

Then bring up the rest:

```bash
docker compose up -d          # postgres, ingest, api, grafana
curl localhost:8080/api/ingest/status    # per-feed health
curl localhost:9091/metrics              # ingest Prometheus metrics
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

## Still to build

- **Phase 6, `web/`** — Vite + React + TS against the OpenAPI document.
- **Phase 7** — wiki Bucket sync for drop tables and item stats, which unlocks the cross-source
  joins (GP/hr, gear comparison) that justify the whole design.
- **Account tracking** — deliberately not built. `plan.md`'s scope decision recommends reading
  Wise Old Man instead, since its snapshot history predates anything this project could start
  collecting. The `players` / `hiscore_snapshots` / `skill_samples` tables remain in the schema
  so the decision stays reversible, and `/api/players/*` is unmapped rather than shipped as
  routes that can only answer 404.

## Before writing any margin logic

GE tax is 2% (raised from 1% on 29 May 2025), capped at 5,000,000 gp per item, with no tax under 50 gp and a real exempt list. It lives in `GrandExchangeTaxRules` as data because it has already moved once. A margin scan that ignores it is wrong, and obviously so to any player.

## Attribution and licence

Price data comes from the [OSRS Wiki real-time prices API](https://oldschool.runescape.wiki/w/RuneScape:Real-time_Prices) and is licensed **CC BY-NC-SA 3.0**. Attribute the wiki. Do not build a commercial product on it. Before running a 24/7 pipeline, say hello in `#api-discussion` on the wiki Discord.

Code in this repo is MIT.
