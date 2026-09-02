using System.Diagnostics;
using System.Text.Json;
using Gielinomics.Client.Hiscores;
using Gielinomics.Client.Json;
using Gielinomics.Data;
using Gielinomics.Ingest.Infrastructure;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Gielinomics.Ingest.Workers;

/// <summary>
/// Polls the official hiscores for each tracked account.
/// </summary>
/// <remarks>
/// <para>
/// An allowlist, never a crawl: only accounts somebody explicitly asked to track are visited.
/// Jagex publishes no rate limit and is markedly less forgiving than the wiki, so requests are
/// spread across the polling interval rather than fired in a burst at the top of the hour.
/// </para>
/// <para>
/// Snapshots are deduplicated by content hash. An account that has not played produces a
/// byte-identical payload, and storing that hourly forever would make this the largest table
/// on disk while telling nobody anything.
/// </para>
/// </remarks>
/// <param name="services">Used to resolve a fresh <see cref="IHiscoresClient"/> per poll.</param>
/// <param name="players">Account storage.</param>
/// <param name="runs">The ingest audit trail.</param>
/// <param name="logger">Log sink.</param>
public sealed class HiscoreWorker(
    IServiceProvider services,
    PlayerRepository players,
    IngestRunRepository runs,
    ILogger<HiscoreWorker> logger) : BackgroundService
{
    /// <summary>The feed name this worker records against.</summary>
    public const string Source = "hiscore";

    /// <summary>How often each account is revisited.</summary>
    private static readonly TimeSpan Interval = TimeSpan.FromHours(1);

    /// <summary>Floor on the gap between two requests, whatever the account count.</summary>
    private static readonly TimeSpan MinimumPacing = TimeSpan.FromSeconds(1);

    /// <summary>How long to idle when nothing is being tracked.</summary>
    private static readonly TimeSpan IdlePoll = TimeSpan.FromMinutes(5);

    /// <inheritdoc />
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        logger.LogInformation("hiscore worker started: revisiting each tracked account every {Interval}.", Interval);

        while (!stoppingToken.IsCancellationRequested)
        {
            IReadOnlyList<Player> tracked;
            try
            {
                tracked = await players.ListTrackedAsync(stoppingToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                return;
            }
            catch (Exception ex)
            {
                logger.LogError(ex, "hiscore: could not read the tracked account list.");
                if (!await DelayAsync(IdlePoll, stoppingToken).ConfigureAwait(false))
                {
                    return;
                }

                continue;
            }

            if (tracked.Count == 0)
            {
                logger.LogDebug("hiscore: nothing tracked; idling.");
                if (!await DelayAsync(IdlePoll, stoppingToken).ConfigureAwait(false))
                {
                    return;
                }

                continue;
            }

            // Spread the accounts evenly across the interval rather than sleeping between
            // sweeps. A continuous walk is self-pacing, never overlaps itself, and picks up
            // newly tracked accounts on the next pass without any extra scheduling.
            var pacing = TimeSpan.FromTicks(Math.Max(MinimumPacing.Ticks, Interval.Ticks / tracked.Count));

            foreach (var player in tracked)
            {
                if (stoppingToken.IsCancellationRequested)
                {
                    return;
                }

                await PollAsync(player, stoppingToken).ConfigureAwait(false);

                if (!await DelayAsync(pacing, stoppingToken).ConfigureAwait(false))
                {
                    return;
                }
            }
        }
    }

    /// <summary>Sleeps, translating shutdown into a false rather than an exception.</summary>
    /// <param name="delay">How long to sleep.</param>
    /// <param name="stoppingToken">Cancels the wait.</param>
    /// <returns>False when shutdown was requested.</returns>
    private static async Task<bool> DelayAsync(TimeSpan delay, CancellationToken stoppingToken)
    {
        try
        {
            await Task.Delay(delay, stoppingToken).ConfigureAwait(false);
            return true;
        }
        catch (OperationCanceledException)
        {
            return false;
        }
    }

    /// <summary>Polls one account and stores the result if it changed.</summary>
    /// <param name="player">The account.</param>
    /// <param name="cancellationToken">Cancels the poll.</param>
    private async Task PollAsync(Player player, CancellationToken cancellationToken)
    {
        using var activity = IngestTelemetry.ActivitySource.StartActivity("poll hiscore");
        activity?.SetTag("gielinomics.player_id", player.Id);

        var stopwatch = Stopwatch.StartNew();

        long runId;
        try
        {
            runId = await runs.BeginAsync(Source, targetBucket: null, cancellationToken).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "hiscore: could not open an ingest run for player {PlayerId}.", player.Id);
            return;
        }

        var outcome = IngestOutcome.UnknownError;
        var rowsWritten = 0;
        string? detail = null;

        try
        {
            var table = Enum.TryParse<HiscoreTable>(player.AccountType, ignoreCase: true, out var parsed)
                ? parsed
                : HiscoreTable.Main;

            var client = services.GetRequiredService<IHiscoresClient>();
            var profile = await client.GetAsync(player.DisplayName, table, cancellationToken).ConfigureAwait(false);

            if (profile is null)
            {
                // A 404 here is real signal, not an error: a hardcore death removes the account
                // from that table permanently, and a rename makes the old name stop resolving.
                outcome = IngestOutcome.Ok;
                detail = $"Not present on the {table} table.";
                logger.LogWarning(
                    "hiscore: '{Player}' is no longer on the {Table} table — a hardcore death, a rename, or a de-iron.",
                    player.DisplayName,
                    table);
                return;
            }

            // The full response is stored, ranks and all, so a future mapping can re-decode it.
            var payloadJson = JsonSerializer.Serialize(profile, GielinomicsJsonContext.Default.HiscoreProfile);

            // The hash covers only what the player did. Rank drifts on its own as the ladder
            // moves around a dormant account, and hashing it would defeat the dedup entirely.
            var contentHash = HiscoreContentHash.Compute(profile);

            var samples = new List<SkillSample>(profile.Skills.Count);
            var capturedAt = DateTimeOffset.UtcNow;

            foreach (var skill in profile.Skills)
            {
                samples.Add(new SkillSample(
                    capturedAt,
                    (short)skill.Id,
                    // -1 means unranked upstream. Stored as null, because an unranked skill is
                    // an absence, not a rank of minus one.
                    skill.Rank < 0 ? null : skill.Rank,
                    skill.Level < 0 ? null : (short)skill.Level,
                    skill.Xp < 0 ? null : skill.Xp));
            }

            var changed = await players.RecordSnapshotAsync(
                player.Id,
                capturedAt,
                payloadJson,
                contentHash,
                HiscoreMapping.Current.Version,
                samples,
                cancellationToken).ConfigureAwait(false);

            rowsWritten = changed ? samples.Count : 0;
            outcome = IngestOutcome.Ok;

            if (!string.Equals(profile.Name, player.DisplayName, StringComparison.Ordinal)
                && await players.ObserveNameAsync(player.Id, profile.Name, cancellationToken).ConfigureAwait(false))
            {
                logger.LogInformation(
                    "hiscore: '{Old}' now reports as '{New}'; name history updated and the timeline kept intact.",
                    player.DisplayName,
                    profile.Name);
            }

            logger.LogDebug(
                "hiscore: '{Player}' {Result}.",
                player.DisplayName,
                changed ? $"changed, {samples.Count} skill samples written" : "unchanged, snapshot touched");
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            detail = "Cancelled by shutdown.";
        }
        catch (Exception ex)
        {
            outcome = ex is HiscoresApiException { InnerException: JsonException }
                ? IngestOutcome.ParseError
                : IngestFailure.Classify(ex);
            detail = IngestFailure.Describe(ex);
            activity?.SetStatus(ActivityStatusCode.Error, detail);

            logger.LogError(ex, "hiscore: poll failed for '{Player}'.", player.DisplayName);
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
                logger.LogError(ex, "hiscore: failed to close ingest run {RunId}.", runId);
            }
        }
    }
}
