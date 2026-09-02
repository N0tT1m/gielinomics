using System.Diagnostics;
using Gielinomics.Client.Prices;
using Gielinomics.Data;
using Gielinomics.Ingest.Infrastructure;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Gielinomics.Ingest.Workers;

/// <summary>
/// Polls an aggregate price feed on its cadence and repairs gaps behind it.
/// </summary>
/// <remarks>
/// One worker type drives both the 5m and 1h feeds, parameterised by <see cref="Feed"/>.
/// Gap repair belongs here in Phase 1: weeks of unaudited ingest produces exactly the
/// untrustworthy dataset the whole project is meant to avoid.
/// </remarks>
/// <param name="feed">Which feed this instance drives.</param>
/// <param name="services">Used to resolve a fresh <see cref="IPricesClient"/> per poll.</param>
/// <param name="prices">Price storage.</param>
/// <param name="runs">The ingest audit trail.</param>
/// <param name="logger">Log sink.</param>
public sealed class PriceSeriesWorker(
    PriceSeriesWorker.Feed feed,
    IServiceProvider services,
    PriceRepository prices,
    IngestRunRepository runs,
    ILogger<PriceSeriesWorker> logger) : BackgroundService
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

    /// <summary>Polls between unprompted gap sweeps.</summary>
    /// <remarks>
    /// Repair already runs on boot and after any failure. This catches the case neither covers:
    /// a poll that reported success against a window the server had not actually filled yet.
    /// </remarks>
    private const int PollsBetweenSweeps = 12;

    /// <summary>Pause between repair requests, so a sweep does not read as a burst upstream.</summary>
    private static readonly TimeSpan RepairPacing = TimeSpan.FromSeconds(1);

    /// <summary>The feed this worker drives.</summary>
    public Feed Configuration => feed;

    /// <inheritdoc />
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        logger.LogInformation(
            "{Source} worker started: every {Interval} at +{Offset}, repairing {RepairWindow} of history.",
            feed.Source,
            feed.Interval,
            feed.Offset,
            feed.RepairWindow);

        // Before the first live poll, not after: a restart is the most likely reason for a
        // hole, and the whole point is that holes never reach the retained dataset.
        await RepairGapsAsync(stoppingToken).ConfigureAwait(false);

        var pollsSinceSweep = 0;

        while (!stoppingToken.IsCancellationRequested)
        {
            if (!await WaitForNextPollAsync(stoppingToken).ConfigureAwait(false))
            {
                return;
            }

            var outcome = await PollAsync(target: null, stoppingToken).ConfigureAwait(false);
            pollsSinceSweep++;

            if (outcome != IngestOutcome.Ok || pollsSinceSweep >= PollsBetweenSweeps)
            {
                await RepairGapsAsync(stoppingToken).ConfigureAwait(false);
                pollsSinceSweep = 0;
            }
        }
    }

    /// <summary>Sleeps until the next scheduled poll.</summary>
    /// <param name="stoppingToken">Cancels the wait.</param>
    /// <returns>False when shutdown was requested during the wait.</returns>
    private async Task<bool> WaitForNextPollAsync(CancellationToken stoppingToken)
    {
        var delay = NextPollAt(DateTimeOffset.UtcNow) - DateTimeOffset.UtcNow;

        try
        {
            if (delay > TimeSpan.Zero)
            {
                await Task.Delay(delay, stoppingToken).ConfigureAwait(false);
            }

            return true;
        }
        catch (OperationCanceledException)
        {
            return false;
        }
    }

    /// <summary>
    /// Polls one window and persists it.
    /// </summary>
    /// <param name="target">
    /// The window to fetch, or null for the most recently completed one. Gap repair passes
    /// an explicit value.
    /// </param>
    /// <param name="cancellationToken">Cancels the poll.</param>
    /// <returns>The recorded outcome.</returns>
    private async Task<string> PollAsync(DateTimeOffset? target, CancellationToken cancellationToken)
    {
        using var activity = IngestTelemetry.ActivitySource.StartActivity($"poll {feed.Source}");
        activity?.SetTag("gielinomics.feed", feed.Source);
        activity?.SetTag("gielinomics.target_bucket", target?.ToString("O"));

        var stopwatch = Stopwatch.StartNew();
        var runId = await runs.BeginAsync(feed.Source, target, cancellationToken).ConfigureAwait(false);

        var outcome = IngestOutcome.UnknownError;
        var rowsWritten = 0;
        string? detail = null;

        try
        {
            // Resolved per poll rather than captured in the constructor. A typed HttpClient
            // held for the life of a singleton never rotates its handler, so it never picks
            // up a DNS change — over a process that runs for months, that matters.
            var client = services.GetRequiredService<IPricesClient>();

            var envelope = feed.StepSeconds == 300
                ? await client.Get5mAsync(target, cancellationToken).ConfigureAwait(false)
                : await client.Get1hAsync(target, cancellationToken).ConfigureAwait(false);

            var bucket = ResolveBucket(envelope.Timestamp, target);

            if (target is { } requested && bucket != requested)
            {
                // Not an error. The server decides which window it served, and persisting it
                // under the window we asked for is how a dataset quietly desynchronises.
                logger.LogInformation(
                    "{Source}: asked for {Requested:O} and was served {Served:O}; storing what was served.",
                    feed.Source,
                    requested,
                    bucket);
            }

            var rows = new List<PriceSeriesRow>(envelope.Data.Count);
            var itemIds = new List<int>(envelope.Data.Count);

            foreach (var (itemId, bar) in envelope.Data)
            {
                itemIds.Add(itemId);
                rows.Add(new PriceSeriesRow(
                    itemId,
                    feed.StepSeconds,
                    bucket,
                    bar.AvgHighPrice,
                    bar.AvgLowPrice,
                    bar.HighPriceVolume,
                    bar.LowPriceVolume,
                    feed.Source));
            }

            // Stub rows first. The hypertables carry no foreign key precisely so a brand-new
            // item ID cannot fail the batch, but items still needs to know the ID exists.
            await prices.EnsureItemsExistAsync(itemIds, cancellationToken).ConfigureAwait(false);
            rowsWritten = await prices.UpsertSeriesAsync(rows, cancellationToken).ConfigureAwait(false);

            outcome = IngestOutcome.Ok;
            activity?.SetTag("gielinomics.rows_written", rowsWritten);

            logger.LogDebug(
                "{Source}: wrote {Rows} rows for window {Bucket:O}.", feed.Source, rowsWritten, bucket);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            outcome = IngestOutcome.UnknownError;
            detail = "Cancelled by shutdown.";
            throw;
        }
        catch (Exception ex)
        {
            outcome = IngestFailure.Classify(ex);
            detail = IngestFailure.Describe(ex);
            activity?.SetStatus(ActivityStatusCode.Error, detail);

            logger.LogError(ex, "{Source}: poll failed for window {Bucket}.", feed.Source, target?.ToString("O") ?? "latest");
        }
        finally
        {
            stopwatch.Stop();

            IngestTelemetry.PollDuration.Record(stopwatch.Elapsed.TotalSeconds, new KeyValuePair<string, object?>("feed", feed.Source));
            IngestTelemetry.Polls.Add(1, new KeyValuePair<string, object?>("feed", feed.Source), new KeyValuePair<string, object?>("outcome", outcome));
            IngestTelemetry.RowsWritten.Add(rowsWritten, new KeyValuePair<string, object?>("feed", feed.Source));

            await CloseRunAsync(runId, outcome, rowsWritten, detail).ConfigureAwait(false);
        }

        return outcome;
    }

    /// <summary>
    /// Closes the audit row, without a cancellation token.
    /// </summary>
    /// <remarks>
    /// Deliberately uncancellable. A shutdown that leaves the row open makes a clean stop
    /// look identical to a crash, which is the one distinction this table exists to preserve.
    /// </remarks>
    /// <param name="runId">The run to close.</param>
    /// <param name="outcome">How it ended.</param>
    /// <param name="rowsWritten">Rows persisted.</param>
    /// <param name="detail">Error text, when it failed.</param>
    private async Task CloseRunAsync(long runId, string outcome, int rowsWritten, string? detail)
    {
        try
        {
            await runs.CompleteAsync(runId, outcome, rowsWritten, detail, CancellationToken.None).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "{Source}: failed to close ingest run {RunId}.", feed.Source, runId);
        }
    }

    /// <summary>
    /// Finds windows missing from the retained history and re-fetches them.
    /// </summary>
    /// <param name="cancellationToken">Cancels the sweep.</param>
    private async Task RepairGapsAsync(CancellationToken cancellationToken)
    {
        List<DateTimeOffset> missing;

        try
        {
            var to = LastCompletedBoundary(DateTimeOffset.UtcNow);
            var from = FloorToStep(to - feed.RepairWindow, feed.StepSeconds);
            var present = await prices.GetPresentBucketsAsync(feed.StepSeconds, from, to, cancellationToken).ConfigureAwait(false);

            missing = [];
            for (var bucket = from; bucket <= to; bucket = bucket.AddSeconds(feed.StepSeconds))
            {
                if (!present.Contains(bucket))
                {
                    missing.Add(bucket);
                }
            }
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            return;
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "{Source}: could not determine which windows are missing.", feed.Source);
            return;
        }

        if (missing.Count == 0)
        {
            logger.LogDebug("{Source}: no gaps in the last {RepairWindow}.", feed.Source, feed.RepairWindow);
            return;
        }

        IngestTelemetry.GapsDetected.Add(missing.Count, new KeyValuePair<string, object?>("feed", feed.Source));
        logger.LogWarning(
            "{Source}: {Count} missing windows in the last {RepairWindow}; repairing oldest first.",
            feed.Source,
            missing.Count,
            feed.RepairWindow);

        foreach (var bucket in missing)
        {
            if (cancellationToken.IsCancellationRequested)
            {
                return;
            }

            var outcome = await PollAsync(bucket, cancellationToken).ConfigureAwait(false);
            if (outcome == IngestOutcome.Ok)
            {
                IngestTelemetry.GapsRepaired.Add(1, new KeyValuePair<string, object?>("feed", feed.Source));
            }

            try
            {
                await Task.Delay(RepairPacing, cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return;
            }
        }
    }

    /// <summary>Decides which window a response actually describes.</summary>
    /// <remarks>
    /// The server's own timestamp wins. It is the only party that knows which window it
    /// served, and a repair request for a window it has aged out will come back as a
    /// different one.
    /// </remarks>
    /// <param name="serverTimestamp">Unix window start from the response, if present.</param>
    /// <param name="requested">The window that was asked for, if any.</param>
    /// <returns>The window to store the rows under.</returns>
    private DateTimeOffset ResolveBucket(long? serverTimestamp, DateTimeOffset? requested)
        => serverTimestamp is { } timestamp
            ? DateTimeOffset.FromUnixTimeSeconds(timestamp)
            : requested ?? LastCompletedBoundary(DateTimeOffset.UtcNow);

    /// <summary>Rounds an instant down to a window boundary.</summary>
    /// <param name="instant">The instant.</param>
    /// <param name="stepSeconds">Window width.</param>
    /// <returns>The start of the window containing the instant.</returns>
    internal static DateTimeOffset FloorToStep(DateTimeOffset instant, int stepSeconds)
        => DateTimeOffset.FromUnixTimeSeconds(instant.ToUnixTimeSeconds() / stepSeconds * stepSeconds);

    /// <summary>The most recent window that has finished.</summary>
    /// <param name="now">Current time.</param>
    /// <returns>The start of the last closed window.</returns>
    internal DateTimeOffset LastCompletedBoundary(DateTimeOffset now)
        => FloorToStep(now, feed.StepSeconds).AddSeconds(-feed.StepSeconds);

    /// <summary>When the next poll should fire.</summary>
    /// <param name="now">Current time.</param>
    /// <returns>The next boundary strictly after <paramref name="now"/>, plus the feed's offset.</returns>
    internal DateTimeOffset NextPollAt(DateTimeOffset now)
    {
        var candidate = FloorToStep(now, feed.StepSeconds).AddSeconds(feed.StepSeconds) + feed.Offset;
        return candidate <= now ? candidate.AddSeconds(feed.StepSeconds) : candidate;
    }
}
