# Gielinomics.Osrs.Client

A typed, async .NET client for the data sources an Old School RuneScape tooling project
actually needs:

- **[OSRS Wiki real-time prices, v2](https://prices.runescape.wiki/api/v2/osrs)** —
  `/mapping`, `/latest`, `/5m`, `/1h`, `/timeseries`.
- **The official hiscores** — every account table, parsed from the CSV endpoint.
- **[Wise Old Man](https://wiseoldman.net)** — player lookups and gains.
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

## The one thing to know before you build on this

`/timeseries` at a one-year lookback returns *daily* bars. There is no fine-grained
historical backfill from any upstream source: 5-minute history only exists from the moment
you start recording it. If you need it, start ingesting today.

## Licence

MIT.
