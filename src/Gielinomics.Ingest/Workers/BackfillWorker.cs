using System.Diagnostics;
using Gielinomics.Client.Prices;
using Gielinomics.Data;
using Gielinomics.Ingest.Infrastructure;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Gielinomics.Ingest.Workers;

/// <summary>
/// One-shot bootstrap pulling a year of <b>daily</b> bars for every item.
/// </summary>
/// <remarks>
/// Not a 5-minute backfill and cannot be made into one: <c>lookback=1y</c> returns 365
/// points at an 86400s step. ~3700 items at 1 req/sec is about an hour, so backfill every
/// item once rather than curating a subset, then leave this disabled.
/// </remarks>
/// <param name="services">Used to resolve a fresh <see cref="IPricesClient"/> per request.</param>
/// <param name="items">Item storage, for the ID list.</param>
/// <param name="prices">Price storage.</param>
/// <param name="runs">The ingest audit trail.</param>
/// <param name="logger">Log sink.</param>
public sealed class BackfillWorker(
    IServiceProvider services,
    ItemRepository items,
    PriceRepository prices,
    IngestRunRepository runs,
    ILogger<BackfillWorker> logger) : BackgroundService
{
    /// <summary>The feed name this worker records against.</summary>
    public const string Source = "timeseries";

    /// <summary>Pause between requests. The wiki asks for reasonable use; one per second is that.</summary>
    private static readonly TimeSpan Pacing = TimeSpan.FromSeconds(1);

    /// <summary>How long to wait for the mapping sync to populate <c>items</c> before giving up.</summary>
    private static readonly TimeSpan MappingWaitTimeout = TimeSpan.FromMinutes(10);

    /// <summary>Interval between checks for a populated item list.</summary>
    private static readonly TimeSpan MappingPollInterval = TimeSpan.FromSeconds(15);

    /// <inheritdoc />
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        logger.LogInformation("backfill worker started. This is a one-shot job; disable it once it completes.");

        var itemIds = await WaitForItemsAsync(stoppingToken).ConfigureAwait(false);
        if (itemIds.Count == 0)
        {
            logger.LogError(
                "backfill: no items known after {Timeout}. The mapping sync has not run yet — leave it to finish and restart with backfill enabled.",
                MappingWaitTimeout);
            return;
        }

        logger.LogInformation(
            "backfill: walking {Count} items at {Rate}/s; expect roughly {Minutes:0} minutes.",
            itemIds.Count,
            1 / Pacing.TotalSeconds,
            itemIds.Count * Pacing.TotalSeconds / 60);

        var succeeded = 0;
        var failed = 0;

        foreach (var itemId in itemIds)
        {
            if (stoppingToken.IsCancellationRequested)
            {
                break;
            }

            if (await BackfillItemAsync(itemId, stoppingToken).ConfigureAwait(false))
            {
                succeeded++;
            }
            else
            {
                failed++;
            }

            try
            {
                await Task.Delay(Pacing, stoppingToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                break;
            }
        }

        logger.LogInformation(
            "backfill complete: {Succeeded} items backfilled, {Failed} failed. Set Gielinomics__RunBackfill=false.",
            succeeded,
            failed);
    }

    /// <summary>
    /// Waits until the item table has something to walk.
    /// </summary>
    /// <remarks>
    /// The mapping sync and this worker start together, and on a fresh database the item list
    /// is empty for the first few seconds. Backfilling nothing and reporting success would be
    /// the quietest possible way to skip the bootstrap entirely.
    /// </remarks>
    /// <param name="stoppingToken">Cancels the wait.</param>
    /// <returns>The item IDs, or an empty list if none appeared in time.</returns>
    private async Task<IReadOnlyList<int>> WaitForItemsAsync(CancellationToken stoppingToken)
    {
        var deadline = DateTimeOffset.UtcNow + MappingWaitTimeout;

        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                var ids = await items.GetAllIdsAsync(stoppingToken).ConfigureAwait(false);
                if (ids.Count > 0)
                {
                    return ids;
                }
            }
            catch (Exception ex)
            {
                logger.LogWarning(ex, "backfill: could not read the item list; retrying.");
            }

            if (DateTimeOffset.UtcNow >= deadline)
            {
                return [];
            }

            try
            {
                await Task.Delay(MappingPollInterval, stoppingToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return [];
            }
        }

        return [];
    }

    /// <summary>Fetches and persists one item's year of daily bars.</summary>
    /// <param name="itemId">The item.</param>
    /// <param name="cancellationToken">Cancels the request.</param>
    /// <returns>True when the item was persisted.</returns>
    private async Task<bool> BackfillItemAsync(int itemId, CancellationToken cancellationToken)
    {
        using var activity = IngestTelemetry.ActivitySource.StartActivity("backfill item");
        activity?.SetTag("gielinomics.item_id", itemId);

        var stopwatch = Stopwatch.StartNew();

        long runId;
        try
        {
            runId = await runs.BeginAsync(Source, targetBucket: null, cancellationToken).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "backfill: could not open an ingest run for item {ItemId}.", itemId);
            return false;
        }

        var outcome = IngestOutcome.UnknownError;
        var rowsWritten = 0;
        string? detail = null;

        try
        {
            var client = services.GetRequiredService<IPricesClient>();
            var response = await client.GetTimeSeriesAsync(itemId, Lookback.OneYear, cancellationToken).ConfigureAwait(false);

            // The server's step, never the one implied by the lookback. It is not contractual
            // and has changed before; storing 365 daily bars as if they were 5-minute ones
            // would corrupt the series in a way no later query could detect.
            var stepSeconds = response.TimeStep;
            if (stepSeconds <= 0)
            {
                throw new PricesApiException($"timeseries for item {itemId} reported a non-positive timestep of {stepSeconds}.");
            }

            var rows = new List<PriceSeriesRow>(response.Data.Count);
            foreach (var point in response.Data)
            {
                rows.Add(new PriceSeriesRow(
                    itemId,
                    stepSeconds,
                    point.TimestampUtc,
                    point.AvgHighPrice,
                    point.AvgLowPrice,
                    point.HighPriceVolume,
                    point.LowPriceVolume,
                    Source));
            }

            await prices.EnsureItemsExistAsync([itemId], cancellationToken).ConfigureAwait(false);
            rowsWritten = await prices.UpsertSeriesAsync(rows, cancellationToken).ConfigureAwait(false);

            outcome = IngestOutcome.Ok;
            return true;
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            detail = "Cancelled by shutdown.";
            return false;
        }
        catch (Exception ex)
        {
            outcome = IngestFailure.Classify(ex);
            detail = IngestFailure.Describe(ex);
            activity?.SetStatus(ActivityStatusCode.Error, detail);

            // One item failing does not stop the walk. A backfill that aborts on the first
            // 404 leaves the other 3699 items unseeded.
            logger.LogWarning(ex, "backfill: item {ItemId} failed.", itemId);
            return false;
        }
        finally
        {
            stopwatch.Stop();

            IngestTelemetry.PollDuration.Record(stopwatch.Elapsed.TotalSeconds, new KeyValuePair<string, object?>("feed", Source));
            IngestTelemetry.Polls.Add(1, new KeyValuePair<string, object?>("feed", Source), new KeyValuePair<string, object?>("outcome", outcome));
            IngestTelemetry.RowsWritten.Add(rowsWritten, new KeyValuePair<string, object?>("feed", Source));

            try
            {
                await runs.CompleteAsync(runId, outcome, rowsWritten, detail, CancellationToken.None).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                logger.LogError(ex, "backfill: failed to close ingest run {RunId}.", runId);
            }
        }
    }
}
