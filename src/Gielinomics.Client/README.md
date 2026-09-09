# Gielinomics.Osrs.Client

A typed, async .NET client for the data sources an Old School RuneScape tooling project
actually needs:

- **[OSRS Wiki real-time prices, v2](https://prices.runescape.wiki/api/v2/osrs)** —
  `/mapping`, `/latest`, `/5m`, `/1h`, `/timeseries`.
- **The official hiscores** — every account table, parsed from the CSV endpoint.
- **[Wise Old Man](https://wiseoldman.net)** — players, gains, snapshots, groups,
  competitions and efficiency rates. Read-only.
- **The wiki's `RuneScape:Bucket`** — drop tables and equipment stats.

Registered through `IHttpClientFactory`, cancellable throughout, and with no dependency on
anything else in the Gielinomics repository — this package is standalone and that repo is
just its first consumer.

```csharp
services.AddGielinomicsClient(options =>
{
    // The wiki blocks default agents outright. Identify yourself and give them a contact.
    options.UserAgent = "my-app/1.0 (github.com/me/my-app)";
});
```

```csharp
var latest = await prices.GetLatestAsync(itemId: 4151, cancellationToken);
var series = await prices.GetTimeseriesAsync(4151, Timestep.FiveMinutes, cancellationToken);
```

## Wise Old Man

`AddGielinomicsWiseOldManClient()` registers it on its own named `HttpClient`, so it gets its
own resilience policy and its own rate limit budget. It needs one: **20 requests per 60
seconds** without an API key — read off the `ratelimit-limit` response header — which is an
order of magnitude tighter than the wiki's prices API, and less than one request per member of
a fifty-person clan. Set `WiseOldManApiKey` to raise it.

```csharp
var player = await wom.GetPlayerAsync("Lynx Titan", cancellationToken);   // null if untracked
var gains  = await wom.GetPlayerGainsAsync("Lynx Titan", WiseOldManPeriod.Week, cancellationToken);
var board  = await wom.GetGroupGainsAsync(groupId: 139, "overall", WiseOldManPeriod.Week, cancellationToken: cancellationToken);
```

Four things about this API are worth knowing before building on it, all verified live:

- **It blocks default agents, exactly as the wiki does.** A request sent as `curl/8.0` gets a
  `403`. The client throws at construction if no User-Agent is set rather than letting you find
  out in production.
- **The window in a `/gained` response is not the window you asked for.** It is bounded by the
  snapshots that exist, so `period=year` on an account first tracked yesterday returns a day.
  `StartsAt` and `EndsAt` are echoed back for exactly this reason; dividing by the period asked
  for overstates the rate by up to 365x.
- **`gained: 0` does not mean "no progress".** An unranked metric reports `-1` at both ends and
  the difference is computed anyway, so "never ranked" and "ranked but idle" are the same zero.
  `MetricDelta.IsRanked` is the check.
- **Snapshots and gains disagree about the same value.** An unranked activity reads `score: 0`
  in a snapshot and `start: -1, end: -1` under `/gained`. Rank is the field that means the same
  thing on both, so `IsRanked` keys off it.

Metric families — skills, bosses, activities, computed — are dictionaries keyed by metric name
rather than records with a property per boss. A new boss then reaches the caller instead of
being dropped silently until this package ships a release.

The client is **read-only**. Wise Old Man's write routes force work onto their infrastructure
and mutate other people's groups; a client that cannot call them cannot spend somebody else's
budget by accident.

## The one thing to know before you build on this

`/timeseries` at a one-year lookback returns *daily* bars. There is no fine-grained
historical backfill from any upstream source: 5-minute history only exists from the moment
you start recording it. If you need it, start ingesting today.

## Licence

MIT.
