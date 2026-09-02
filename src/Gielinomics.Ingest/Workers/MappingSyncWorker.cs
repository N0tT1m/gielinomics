using System.Diagnostics;
using Gielinomics.Client.Prices;
using Gielinomics.Data;
using Gielinomics.Ingest.Infrastructure;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Gielinomics.Ingest.Workers;

/// <summary>Refreshes item reference data from <c>/mapping</c> daily.</summary>
/// <param name="services">Used to resolve a fresh <see cref="IPricesClient"/> per sync.</param>
/// <param name="items">Item storage.</param>
/// <param name="runs">The ingest audit trail.</param>
/// <param name="logger">Log sink.</param>
public sealed class MappingSyncWorker(
    IServiceProvider services,
    ItemRepository items,
    IngestRunRepository runs,
    ILogger<MappingSyncWorker> logger) : BackgroundService
{
    /// <summary>The feed name this worker records against.</summary>
    public const string Source = "mapping";

    /// <summary>Sync cadence.</summary>
    private static readonly TimeSpan Interval = TimeSpan.FromDays(1);

    /// <inheritdoc />
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        logger.LogInformation("mapping worker started: syncing every {Interval}.", Interval);

        using var timer = new PeriodicTimer(Interval);

        do
        {
            await SyncAsync(stoppingToken).ConfigureAwait(false);
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

    /// <summary>Fetches the mapping and upserts it.</summary>
    /// <param name="cancellationToken">Cancels the sync.</param>
    private async Task SyncAsync(CancellationToken cancellationToken)
    {
        using var activity = IngestTelemetry.ActivitySource.StartActivity("sync mapping");
        var stopwatch = Stopwatch.StartNew();

        long runId;
        try
        {
            runId = await runs.BeginAsync(Source, targetBucket: null, cancellationToken).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "mapping: could not open an ingest run; skipping this sync.");
            return;
        }

        var outcome = IngestOutcome.UnknownError;
        var rowsWritten = 0;
        string? detail = null;

        try
        {
            var client = services.GetRequiredService<IPricesClient>();
            var mapping = await client.GetMappingAsync(cancellationToken).ConfigureAwait(false);

            WarnOnSchemaDrift(mapping);

            var rows = new List<ItemRow>(mapping.Count);
            foreach (var item in mapping)
            {
                rows.Add(new ItemRow(
                    item.Id,
                    item.Name,
                    item.Examine,
                    item.Members,
                    item.Limit,
                    item.Value,
                    item.LowAlch,
                    item.HighAlch,
                    item.Icon));
            }

            rowsWritten = await items.UpsertAsync(rows, cancellationToken).ConfigureAwait(false);
            outcome = IngestOutcome.Ok;

            logger.LogInformation("mapping: upserted {Rows} items.", rowsWritten);
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

            logger.LogError(ex, "mapping: sync failed.");
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
                logger.LogError(ex, "mapping: failed to close ingest run {RunId}.", runId);
            }
        }
    }

    /// <summary>
    /// Logs when the mapping carries fields this client does not model.
    /// </summary>
    /// <remarks>
    /// A field nobody notices is a field nobody retains, and the retained dataset is the whole
    /// asset. This is the cheap half of the schema drift alarm: the client already captured the
    /// unknown members, so all that is left is to refuse to stay quiet about them.
    /// </remarks>
    /// <param name="mapping">The mapping just fetched.</param>
    private void WarnOnSchemaDrift(IReadOnlyList<ItemMapping> mapping)
    {
        var unknownFields = new HashSet<string>(StringComparer.Ordinal);

        foreach (var item in mapping)
        {
            if (item.AdditionalData is { Count: > 0 } extra)
            {
                foreach (var key in extra.Keys)
                {
                    unknownFields.Add(key);
                }
            }
        }

        if (unknownFields.Count > 0)
        {
            logger.LogError(
                "mapping: response carries {Count} unmodelled field(s) — {Fields}. The upstream schema has moved; ItemMapping needs updating before this data is lost.",
                unknownFields.Count,
                string.Join(", ", unknownFields.Order(StringComparer.Ordinal)));
        }
    }
}
