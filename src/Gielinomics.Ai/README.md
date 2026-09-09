# Gielinomics.Ai

The answering layer: ask about Old School RuneScape in plain English, get an answer with citations.

Python, in a repository that is otherwise C#, and deliberately so. The retrieval index is a
54 MB float32 matrix scanned with numpy, the embeddings come from `fastembed`, and the agent
loop is 3,700 lines of enforcement passes built around those two. That is where Python's
ecosystem is load-bearing; the ingest workers, the API and the alerting are where .NET is, and
neither half is improved by being rewritten in the other's language.

The package is still importable as `reldo`, which is what it was called before it moved here.
Renaming it would have touched every import in 33 modules and 30 test files to say the same
thing a directory name already says.

## What it does

| Surface | Entry point | Notes |
| --- | --- | --- |
| Discord bot | `reldo bot` | 18 slash commands, plus conversation on mention/reply/DM |
| Ask once | `reldo ask "..."` | No Discord needed |
| Search | `reldo search "..."` | Retrieval only, no model — what the index returns for a query |
| Trend | `reldo trend <item>` | What a price has been doing. Needs the platform |
| HTTP | `reldo serve` | `/api/ai/*`, for the web frontend |
| Coach | `reldo coach` | Unprompted remarks against live RuneLite state |

Most of the slash commands never touch the model. `/ge`, `/trend`, `/xp`, `/stats` and `/search`
compute their answers in code and print them, because a model asked to do arithmetic on prices
will do arithmetic on prices.

No personas ship with this repository. `reldo/persona.py` keeps the hook — so a voice added
later inherits the grounding clause rather than bypassing every enforcement pass — and defines
only the neutral one.

## How it reads the game's data

Every client here was written against a public API — the wiki's real-time prices, Jagex's
hiscores, Wise Old Man. Each answers *what is true right now* and keeps no history.

Set `RELDO_GIELINOMICS_URL` and the price and hiscores clients read from this repository's own
platform instead: the TimescaleDB the ingest workers have been filling with five-minute price
bars and hiscore snapshots. That is what makes "has the whip been rising" answerable — the
upstream APIs keep nothing to answer it from.

`reldo/gielinomics.py` is the whole integration, and it is three subclasses that change where
the bytes come from and nothing else:

| Class | Overrides | Inherited unchanged |
| --- | --- | --- |
| `GEClient` | `_get` | name resolution, ranking, tax, liquidity, spread sanity |
| ↳ adds `trend()` | — | the question the upstream APIs cannot answer at all |
| `HiscoresClient` | `lookup` | combat level, `meets`, activities, `summary` |
| `WomClient` | `gains` | `lookup` (EHP/EHB), `track` |

It works because the C# API serves the *upstream shape* on `/api/prices/{mapping,latest,24h}`
and `/api/players/{name}/snapshot`, rather than making this side translate. The alternative was
reimplementing six hundred lines of judgement against a second set of field names, in a second
language, and getting it subtly different in one of them.

`reldo/clients.py` makes the "platform or upstream" decision once, from the settings. Nothing
else in the package tests that setting, so there is no path where the agent quotes a platform
price while `/ge` quotes the wiki's and the two disagree inside one conversation.

**Unset `RELDO_GIELINOMICS_URL` and everything points upstream again.** The package stays usable
standalone, and a platform that is down costs you the history, not the price — every method
falls back and logs why.

**What deliberately does not route through the platform.** WOM's efficiency model (EHP, EHB,
time-to-max) is a community ratings system, not an observation. The platform does not compute
it, so proxying the call would add a hop and a failure mode to reach the same WOM response.

## Running it

Inside the compose stack it comes up with everything else:

```sh
docker compose up ai
```

**The index volume starts empty**, so search is unavailable until you fill it once:

```sh
docker compose run --rm ai reldo build
```

Until then the service still comes up and reports `search: false` on `/health`, and the search
route 503s with that command in the body. It does not exit: under `restart: unless-stopped` that
would be a crash loop that never produces an index.

Standalone, for development:

```sh
cd src/Gielinomics.Ai
uv sync
uv run reldo ask "is the whip worth it at 70 attack"
```

## The index

`reldo build` writes `data/index.npz` and `data/chunks.npz` — about 330 MB of embeddings across
~35k wiki articles. **It is a build artifact and is not in git.** Point `RELDO_INDEX_PATH` at a
copy you already have, or build one:

```sh
uv run reldo build             # lead-paragraph index, ~20 minutes
uv run reldo build --chunks    # passage index, longer
```

Search and the `/wiki` command need it. Everything that computes its answer in code — `/ge`,
`/trend`, `/xp`, `/stats` — works on a fresh clone without it.

## Configuration

Settings load from the environment or a `.env`, all prefixed `RELDO_`. See `.env.example`, and
`reldo/config.py` for what each one means. The ones that matter here:

| Variable | Default | What it does |
| --- | --- | --- |
| `RELDO_USER_AGENT` | *(required)* | The wiki 403s an unset agent. Include a contact URL. |
| `RELDO_GIELINOMICS_URL` | *(empty)* | Read prices and stats from the platform. Empty means upstream. |
| `RELDO_GIELINOMICS_TOKEN` | *(empty)* | Needed only to enrol accounts for tracking. |
| `RELDO_GIELINOMICS_FALLBACK` | `true` | Ask upstream when the platform cannot answer. |
| `RELDO_INDEX_PATH` | `data/index.npz` | Where the embeddings live. |
| `RELDO_DISCORD_TOKEN` | *(empty)* | Only for `reldo bot`. |

## Tests

```sh
uv run pytest
```

No network: every client is exercised through an `httpx` mock transport.
