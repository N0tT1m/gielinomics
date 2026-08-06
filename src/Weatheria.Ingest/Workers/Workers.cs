using Microsoft.Extensions.Hosting;

namespace Weatheria.Ingest.Workers;

/// <summary>
/// Polls an aggregate price feed on its cadence and repairs gaps behind it.
/// </summary>
/// <remarks>
/// One worker type drives both the 5m and 1h feeds, parameterised by <see cref="Feed"/>.
/// Gap repair belongs here in Phase 1: weeks of unaudited ingest produces exactly the
/// untrustworthy dataset the whole project is meant to avoid.
/// </remarks>
/// <param name="feed">Which feed this instance drives.</param>
public sealed class PriceSeriesWorker(PriceSeriesWorker.Feed feed) : BackgroundService
{
    /// <summary>Cadence and granularity for one aggregate feed.</summary>
    /// <param name="Source">Feed name, used as the <c>ingest_runs.source</c> value.</param>
    /// <param name="StepSeconds">Window width.</param>
    /// <param name="Interval">Poll interval.</param>
    /// <param name="Offset">How far past the boundary to poll, so the window has closed.</param>
    /// <param name="RepairWindow">How far back gap repair reaches per sweep.</param>
    public readonly record struct Feed(string Source, int StepSeconds, TimeSpan Interval, TimeSpan Offset, TimeSpan RepairWindow)
    {
        /// <summary>The 5-minute feed.</summary>
        public static Feed FiveMinute { get; } = new("5m", 300, TimeSpan.FromMinutes(5), TimeSpan.FromSeconds(30), TimeSpan.FromHours(6));

        /// <summary>The hourly feed.</summary>
        public static Feed Hourly { get; } = new("1h", 3600, TimeSpan.FromHours(1), TimeSpan.FromMinutes(1), TimeSpan.FromDays(2));
    }

    /// <summary>The feed this worker drives.</summary>
    public Feed Configuration => feed;

    /// <inheritdoc />
    protected override Task ExecuteAsync(CancellationToken stoppingToken)
        => throw new NotImplementedException(
            "TODO: repair gaps on boot, then poll on PeriodicTimer(feed.Interval). " +
            "Open an ingest_runs row per attempt. Trust the server's window start over the requested one.");
}

/// <summary>
/// Polls <c>/latest</c> every 60s and appends only items whose trade timestamps moved.
/// </summary>
/// <remarks>
/// The change gate is the point. Writing every response wholesale is ~5.3M rows/day,
/// five times the 5m series, almost all byte-identical duplicates.
/// </remarks>
public sealed class LatestPriceWorker : BackgroundService
{
    /// <inheritdoc />
    protected override Task ExecuteAsync(CancellationToken stoppingToken)
        => throw new NotImplementedException(
            "TODO: poll /latest every 60s; keep last (highTime, lowTime) per item in memory " +
            "and write only the items that changed.");
}

/// <summary>Refreshes item reference data from <c>/mapping</c> daily.</summary>
public sealed class MappingSyncWorker : BackgroundService
{
    /// <inheritdoc />
    protected override Task ExecuteAsync(CancellationToken stoppingToken)
        => throw new NotImplementedException(
            "TODO: daily /mapping upsert. Alert when the response carries fields outside the known set.");
}

/// <summary>
/// Escalates when a feed stops producing successful runs.
/// </summary>
/// <remarks>
/// Silent death is the realistic failure mode of a 24/7 poller: process up, logs quiet,
/// nobody notices until someone queries a month-old hole. Phase 1, not Phase 5.
/// </remarks>
public sealed class StalenessMonitor : BackgroundService
{
    /// <summary>Feeds watched, and how long each may go without a success.</summary>
    public static (string Source, TimeSpan Threshold)[] Watched { get; } =
    [
        ("5m", TimeSpan.FromMinutes(15)),
        ("latest", TimeSpan.FromMinutes(5)),
        ("1h", TimeSpan.FromHours(3)),
    ];

    /// <inheritdoc />
    protected override Task ExecuteAsync(CancellationToken stoppingToken)
        => throw new NotImplementedException(
            "TODO: every 5 min, check TimeSinceLastSuccessAsync per feed and log at Error past threshold " +
            "so Grafana alerts on it without needing Phase 5.");
}

/// <summary>
/// One-shot bootstrap pulling a year of <b>daily</b> bars for every item.
/// </summary>
/// <remarks>
/// Not a 5-minute backfill and cannot be made into one: <c>lookback=1y</c> returns 365
/// points at an 86400s step. ~3700 items at 1 req/sec is about an hour, so backfill every
/// item once rather than curating a subset, then leave this disabled.
/// </remarks>
public sealed class BackfillWorker : BackgroundService
{
    /// <inheritdoc />
    protected override Task ExecuteAsync(CancellationToken stoppingToken)
        => throw new NotImplementedException(
            "TODO: walk all item IDs at 1 req/sec, persisting with the server-reported timestep.");
}
