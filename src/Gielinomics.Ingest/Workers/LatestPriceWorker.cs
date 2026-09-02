using System.Diagnostics;
using Gielinomics.Client.Prices;
using Gielinomics.Data;
using Gielinomics.Ingest.Infrastructure;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Gielinomics.Ingest.Workers;

/// <summary>
/// Polls <c>/latest</c> every 60s and appends only items whose trade timestamps moved.
/// </summary>
/// <remarks>
/// The change gate is the point. Writing every response wholesale is ~5.3M rows/day,
/// five times the 5m series, almost all byte-identical duplicates.
/// </remarks>
/// <param name="services">Used to resolve a fresh <see cref="IPricesClient"/> per poll.</param>
/// <param name="prices">Price storage.</param>
/// <param name="runs">The ingest audit trail.</param>
/// <param name="logger">Log sink.</param>
public sealed class LatestPriceWorker(
    IServiceProvider services,
    PriceRepository prices,
    IngestRunRepository runs,
    ILogger<LatestPriceWorker> logger) : BackgroundService
{
    /// <summary>The feed name this worker records against.</summary>
    public const string Source = "latest";

    /// <summary>Poll cadence.</summary>
    private static readonly TimeSpan Interval = TimeSpan.FromMinutes(1);

    /// <summary>
    /// The last trade timestamps seen per item.
    /// </summary>
    /// <remarks>
    /// In memory, not in the database. A restart re-writes one full snapshot — about 3700
    /// rows, once — which is a far better trade than a query against the newest row of every
    /// item on every 60-second tick.
    /// </remarks>
    private readonly Dictionary<int, (long? HighTime, long? LowTime)> _lastSeen = [];

    /// <inheritdoc />
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        logger.LogInformation("latest worker started: polling every {Interval}.", Interval);

        using var timer = new PeriodicTimer(Interval);

        do
        {
            await PollAsync(stoppingToken).ConfigureAwait(false);
        }
        while (await SafeWaitAsync(timer, stoppingToken).ConfigureAwait(false));
    }

    /// <summary>Waits for the next tick, translating shutdown into a false rather than an exception.</summary>
    /// <param name="timer">The cadence timer.</param>
    /// <param name="stoppingToken">Cancels the wait.</param>
    /// <returns>False when shutdown was requested.</returns>
    private static async Task<bool> SafeWaitAsync(PeriodicTimer timer, CancellationToken stoppingToken)
    {
        try
        {
            return await timer.WaitForNextTickAsync(stoppingToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            return false;
        }
    }

    /// <summary>Polls once and appends the rows that changed.</summary>
    /// <param name="cancellationToken">Cancels the poll.</param>
    private async Task PollAsync(CancellationToken cancellationToken)
    {
        using var activity = IngestTelemetry.ActivitySource.StartActivity("poll latest");
        var stopwatch = Stopwatch.StartNew();

        long runId;
        try
        {
            runId = await runs.BeginAsync(Source, targetBucket: null, cancellationToken).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "latest: could not open an ingest run; skipping this tick.");
            return;
        }

        var outcome = IngestOutcome.UnknownError;
        var rowsWritten = 0;
        string? detail = null;

        try
        {
            var client = services.GetRequiredService<IPricesClient>();
            var latest = await client.GetLatestAsync(cancellationToken).ConfigureAwait(false);

            // One observation time for the whole batch. The individual trades already carry
            // their own timestamps; observed_at records when this platform looked, and the
            // batch looked once.
            var observedAt = DateTimeOffset.UtcNow;

            var changed = new List<PriceLatestRow>();
            var changedIds = new List<int>();

            foreach (var (itemId, price) in latest)
            {
                var current = (price.HighTime, price.LowTime);

                if (_lastSeen.TryGetValue(itemId, out var previous) && previous == current)
                {
                    continue;
                }

                _lastSeen[itemId] = current;
                changedIds.Add(itemId);
                changed.Add(new PriceLatestRow(
                    itemId,
                    observedAt,
                    price.High,
                    price.HighTimeUtc,
                    price.Low,
                    price.LowTimeUtc));
            }

            if (changed.Count > 0)
            {
                await prices.EnsureItemsExistAsync(changedIds, cancellationToken).ConfigureAwait(false);
                rowsWritten = await prices.InsertLatestAsync(changed, cancellationToken).ConfigureAwait(false);
            }

            outcome = IngestOutcome.Ok;
            activity?.SetTag("gielinomics.rows_written", rowsWritten);

            logger.LogDebug(
                "latest: {Changed} of {Total} items moved; wrote {Rows} rows.",
                changed.Count,
                latest.Count,
                rowsWritten);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            detail = "Cancelled by shutdown.";
        }
        catch (Exception ex)
        {
            outcome = IngestFailure.Classify(ex);
            detail = IngestFailure.Describe(ex);
            activity?.SetStatus(ActivityStatusCode.Error, detail);

            // The in-memory gate is not rolled back on failure. An item marked as seen whose
            // row did not persist would be skipped until it next trades, so drop the whole
            // gate and let the next tick rewrite a full snapshot.
            _lastSeen.Clear();

            logger.LogError(ex, "latest: poll failed; change gate reset so the next tick rewrites a full snapshot.");
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
                logger.LogError(ex, "latest: failed to close ingest run {RunId}.", runId);
            }
        }
    }
}
