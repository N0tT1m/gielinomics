# Weatheria

A time-series service that retains OSRS market history the official APIs don't, with a query API and alerting layer on top.

Named for the sky island whose entire purpose is accumulating long-run observation data to forecast what's coming.

> **Status: scaffold.** Structure, contracts and schema are in place; the implementations are `NotImplementedException` stubs marked with `TODO`. See [`plan.md`](plan.md) for the full design.

**The moat is the dataset.** The upstream APIs serve recent windows only, and `/timeseries` at a one-year lookback returns *daily* bars — there is no fine-grained backfill. The 5-minute series can only ever start the day ingest does, which is why Phase 1 is "turn the worker on and leave it running".

## Layout

```
src/
├── Weatheria.Client/    # NuGet: Weatheria.Osrs.Client. References nothing else here.
├── Weatheria.Data/      # Npgsql + Dapper repositories
├── Weatheria.Ingest/    # BackgroundServices: poll, gap-repair, backfill, staleness
├── Weatheria.Api/       # ASP.NET Core minimal API
└── Weatheria.Alerts/    # Rule evaluation, GE tax, webhook validation
tests/
├── Weatheria.Client.Tests/   # recorded fixtures, no network in CI
└── Weatheria.Api.Tests/
db/init/                 # schema, applied on first container start
web/                     # Phase 6: Vite + React + TS
```

`Weatheria.Client` must not reference any other project in this solution. It's a standalone package that happens to have this repo as its first consumer.

## Running

```bash
cp .env.example .env      # fill in POSTGRES_PASSWORD and WEATHERIA_USER_AGENT
docker compose up -d postgres
dotnet build
dotnet test
```

`db/init/01_schema.sql` is applied by the container on first start only. If the schema
changes, `docker compose down -v` to drop the volume and let it re-apply.

### Running the API or ingest worker outside Docker

Compose injects `ConnectionStrings__Weatheria` as an environment variable; a bare
`dotnet run` or an IDE launch gets nothing, and `appsettings.json` holds an empty
placeholder. Put the value in user secrets once per project:

```bash
# Host port is 5433 -- 5432 is left to any native Postgres install.
CS="Host=localhost;Port=5433;Database=weatheria;Username=weatheria;Password=$POSTGRES_PASSWORD"
dotnet user-secrets set "ConnectionStrings:Weatheria" "$CS" --project src/Weatheria.Api
dotnet user-secrets set "ConnectionStrings:Weatheria" "$CS" --project src/Weatheria.Ingest
dotnet user-secrets set "Weatheria:UserAgent" "$WEATHERIA_USER_AGENT" --project src/Weatheria.Ingest
```

User secrets load **only** in the Development environment. The `launchSettings.json`
profiles set it; if you run the built binary directly, export `ASPNETCORE_ENVIRONMENT=Development`
(`DOTNET_ENVIRONMENT` for the ingest worker) or the host falls back to the empty
placeholder and throws at startup.

`WEATHERIA_USER_AGENT` is **required**, not advisory. The wiki pre-emptively blocks a list of default agents — including `RestSharp`, `python-requests` and `curl` — so the client throws at construction rather than letting you discover it as a 403 storm in production. Use something like `weatheria/0.1 (github.com/N0tT1m/weatheria)`.

## Build order

Follow the phases in `plan.md`. The parts that cannot be added retroactively, and therefore ship in Phase 1 with the first worker:

- `ingest_runs` written on every poll attempt — the only thing distinguishing a quiet market from a dead worker
- gap detection and backwards repair via the `timestamp` parameter
- a staleness alarm at 15 minutes with no new bucket

Everything else can wait. These three are what make the accumulated history trustworthy, and history nobody trusts is not a moat.

## Before writing any margin logic

GE tax is 2% (raised from 1% on 29 May 2025), capped at 5,000,000 gp per item, with no tax under 50 gp and a real exempt list. It lives in `GrandExchangeTaxRules` as data because it has already moved once. A margin scan that ignores it is wrong, and obviously so to any player.

## Attribution and licence

Price data comes from the [OSRS Wiki real-time prices API](https://oldschool.runescape.wiki/w/RuneScape:Real-time_Prices) and is licensed **CC BY-NC-SA 3.0**. Attribute the wiki. Do not build a commercial product on it. Before running a 24/7 pipeline, say hello in `#api-discussion` on the wiki Discord.

Code in this repo is MIT.
