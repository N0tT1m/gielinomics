using Gielinomics.Alerts;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Gielinomics.Ingest.Workers;

/// <summary>
/// Sweeps the alert rules on a fixed cadence.
/// </summary>
/// <remarks>
/// Hosted in the worker rather than the API. Every other scheduled job lives here, and an API
/// that is scaled to more than one replica would otherwise evaluate — and fire — each rule
/// once per replica.
/// </remarks>
/// <param name="evaluator">The rule evaluator.</param>
/// <param name="options">Sweep cadence.</param>
/// <param name="logger">Log sink.</param>
public sealed class AlertEvaluationWorker(
    AlertEvaluator evaluator,
    AlertEvaluatorOptions options,
    ILogger<AlertEvaluationWorker> logger) : BackgroundService
{
    /// <inheritdoc />
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        logger.LogInformation("alert evaluator started: sweeping every {Interval}.", options.SweepInterval);

        using var timer = new PeriodicTimer(options.SweepInterval);

        while (await SafeWaitAsync(timer, stoppingToken).ConfigureAwait(false))
        {
            try
            {
                var fired = await evaluator.EvaluateAllAsync(stoppingToken).ConfigureAwait(false);
                if (fired > 0)
                {
                    logger.LogInformation("alert evaluator: {Fired} rule(s) fired.", fired);
                }
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                return;
            }
            catch (Exception ex)
            {
                logger.LogError(ex, "alert evaluator: sweep failed.");
            }
        }
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
}
