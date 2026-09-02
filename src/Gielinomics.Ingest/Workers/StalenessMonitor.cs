using Gielinomics.Data;
using Gielinomics.Ingest.Infrastructure;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Gielinomics.Ingest.Workers;

/// <summary>
/// Escalates when a feed stops producing successful runs.
/// </summary>
/// <remarks>
/// Silent death is the realistic failure mode of a 24/7 poller: process up, logs quiet,
/// nobody notices until someone queries a month-old hole. Phase 1, not Phase 5.
/// </remarks>
/// <param name="runs">The ingest audit trail.</param>
/// <param name="logger">Log sink.</param>
public sealed class StalenessMonitor(IngestRunRepository runs, ILogger<StalenessMonitor> logger) : BackgroundService
{
    /// <summary>Feeds watched, and how long each may go without a success.</summary>
    public static (string Source, TimeSpan Threshold)[] Watched { get; } =
    [
        ("5m", TimeSpan.FromMinutes(15)),
        ("latest", TimeSpan.FromMinutes(5)),
        ("1h", TimeSpan.FromHours(3)),
    ];

    /// <summary>How often the thresholds are checked.</summary>
    private static readonly TimeSpan Interval = TimeSpan.FromMinutes(5);

    /// <summary>
    /// How long after startup a feed that has never succeeded is tolerated.
    /// </summary>
    /// <remarks>
    /// A fresh database has no successful run for any feed, and alarming on that would mean
    /// the very first deployment pages someone. The grace period covers the hourly feed's
    /// first poll, which is the slowest to arrive.
    /// </remarks>
    private static readonly TimeSpan StartupGrace = TimeSpan.FromHours(2);

    private readonly DateTimeOffset _startedAt = DateTimeOffset.UtcNow;

    /// <inheritdoc />
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        logger.LogInformation("staleness monitor started: checking every {Interval}.", Interval);

        using var timer = new PeriodicTimer(Interval);

        do
        {
            await CheckAsync(stoppingToken).ConfigureAwait(false);
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

    /// <summary>Checks every watched feed against its threshold.</summary>
    /// <param name="cancellationToken">Cancels the check.</param>
    private async Task CheckAsync(CancellationToken cancellationToken)
    {
        foreach (var (source, threshold) in Watched)
        {
            try
            {
                var age = await runs.TimeSinceLastSuccessAsync(source, cancellationToken).ConfigureAwait(false);
                var tag = new KeyValuePair<string, object?>("feed", source);

                if (age is null)
                {
                    // Published as the age since startup rather than left unset. An absent
                    // series and a healthy one look identical on a dashboard.
                    var sinceStart = DateTimeOffset.UtcNow - _startedAt;
                    IngestTelemetry.StalenessSeconds.Record(sinceStart.TotalSeconds, tag);

                    if (sinceStart > StartupGrace)
                    {
                        logger.LogError(
                            "STALE: feed '{Source}' has never completed successfully, {Age} after startup.",
                            source,
                            sinceStart);
                    }

                    continue;
                }

                IngestTelemetry.StalenessSeconds.Record(age.Value.TotalSeconds, tag);

                // Error level on purpose. Grafana alerts on this without needing the Phase 5
                // alerting layer to exist, which is what makes day-one staleness detection real.
                if (age > threshold)
                {
                    logger.LogError(
                        "STALE: feed '{Source}' last succeeded {Age} ago, past its {Threshold} threshold.",
                        source,
                        age,
                        threshold);
                }
                else
                {
                    logger.LogDebug("feed '{Source}' last succeeded {Age} ago.", source, age);
                }
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                return;
            }
            catch (Exception ex)
            {
                logger.LogError(ex, "staleness monitor: could not check feed '{Source}'.", source);
            }
        }
    }
}
