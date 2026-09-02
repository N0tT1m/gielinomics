using System.Diagnostics;
using System.Text.Json;
using Gielinomics.Client.Json;
using Gielinomics.Client.Wiki;
using Gielinomics.Data;
using Gielinomics.Ingest.Infrastructure;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Gielinomics.Ingest.Workers;

/// <summary>
/// Syncs the wiki's structured data — equipment bonuses, drop tables, monsters — weekly.
/// </summary>
/// <remarks>
/// <para>
/// This is the cross-source join the whole design is for. Prices alone answer "what does a whip
/// cost"; prices plus drop tables answer "what is an abyssal demon kill worth", and prices plus
/// equipment bonuses answer "what is the cheapest strength bonus in this slot". Neither upstream
/// API can answer either.
/// </para>
/// <para>
/// Near-static reference data, so weekly is generous. Each bucket is replaced wholesale in one
/// transaction: Bucket exposes no row identity to upsert against, and a full reload of a few
/// tens of thousands of rows is both simpler and incapable of drifting.
/// </para>
/// </remarks>
/// <param name="services">Used to resolve a fresh <see cref="IWikiBucketClient"/> per sync.</param>
/// <param name="wiki">Wiki data storage.</param>
/// <param name="runs">The ingest audit trail.</param>
/// <param name="logger">Log sink.</param>
public sealed class BucketSyncWorker(
    IServiceProvider services,
    WikiRepository wiki,
    IngestRunRepository runs,
    ILogger<BucketSyncWorker> logger) : BackgroundService
{
    /// <summary>The feed name this worker records against.</summary>
    public const string Source = "bucket";

    /// <summary>Sync cadence.</summary>
    private static readonly TimeSpan Interval = TimeSpan.FromDays(7);

    /// <summary>Rows per Bucket request. The API serves 5000 comfortably.</summary>
    private const int PageSize = 5000;

    /// <summary>How long to wait for the item mapping before syncing anyway.</summary>
    private static readonly TimeSpan MappingWaitTimeout = TimeSpan.FromMinutes(10);

    /// <summary>Interval between checks for a populated mapping.</summary>
    private static readonly TimeSpan MappingPollInterval = TimeSpan.FromSeconds(15);

    /// <inheritdoc />
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        logger.LogInformation("bucket worker started: syncing wiki structured data every {Interval}.", Interval);

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

    /// <summary>Runs one full sync of every bucket this platform reads.</summary>
    /// <param name="cancellationToken">Cancels the sync.</param>
    private async Task SyncAsync(CancellationToken cancellationToken)
    {
        using var activity = IngestTelemetry.ActivitySource.StartActivity("sync bucket");
        var stopwatch = Stopwatch.StartNew();

        long runId;
        try
        {
            runId = await runs.BeginAsync(Source, targetBucket: null, cancellationToken).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "bucket: could not open an ingest run; skipping this sync.");
            return;
        }

        var outcome = IngestOutcome.UnknownError;
        var rowsWritten = 0;
        string? detail = null;

        try
        {
            // Wiki rows resolve their item IDs by joining on items.name, so the mapping has to
            // be there first. On a fresh database both workers start at once and this one is
            // the faster of the two.
            await WaitForMappingAsync(cancellationToken).ConfigureAwait(false);

            var client = services.GetRequiredService<IWikiBucketClient>();

            rowsWritten += await SyncBonusesAsync(client, cancellationToken).ConfigureAwait(false);
            rowsWritten += await SyncMonstersAsync(client, cancellationToken).ConfigureAwait(false);
            rowsWritten += await SyncDropsAsync(client, cancellationToken).ConfigureAwait(false);

            // After every load, never per-bucket: resolution must not depend on how far the
            // item mapping had got when any one bucket happened to land.
            var resolved = await wiki.ResolveItemIdsAsync(cancellationToken).ConfigureAwait(false);
            if (resolved > 0)
            {
                logger.LogInformation("bucket: resolved item IDs for {Rows} rows that the load could not match.", resolved);
            }

            outcome = IngestOutcome.Ok;
            logger.LogInformation("bucket: sync complete, {Rows} rows written.", rowsWritten);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            detail = "Cancelled by shutdown.";
        }
        catch (Exception ex)
        {
            outcome = ex is WikiApiException { InnerException: JsonException }
                ? IngestOutcome.ParseError
                : ex is WikiApiException
                    ? IngestOutcome.HttpError
                    : IngestFailure.Classify(ex);
            detail = IngestFailure.Describe(ex);
            activity?.SetStatus(ActivityStatusCode.Error, detail);

            logger.LogError(ex, "bucket: sync failed.");
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
                logger.LogError(ex, "bucket: failed to close ingest run {RunId}.", runId);
            }
        }
    }

    /// <summary>
    /// Waits for the mapping sync to have completed successfully at least once.
    /// </summary>
    /// <remarks>
    /// The signal is a successful <c>mapping</c> run in the audit trail, not a row count.
    /// Counting was the obvious thing and it was wrong: a handful of leftover rows satisfies
    /// "greater than zero" while the real mapping is still downloading, and the sync then
    /// resolves almost nothing. The audit trail is the only thing that actually knows whether
    /// the mapping has landed.
    /// </remarks>
    /// <param name="cancellationToken">Cancels the wait.</param>
    private async Task WaitForMappingAsync(CancellationToken cancellationToken)
    {
        var deadline = DateTimeOffset.UtcNow + MappingWaitTimeout;

        while (!cancellationToken.IsCancellationRequested)
        {
            try
            {
                if (await runs.TimeSinceLastSuccessAsync(MappingSyncWorker.Source, cancellationToken).ConfigureAwait(false) is not null)
                {
                    return;
                }
            }
            catch (Exception ex)
            {
                logger.LogWarning(ex, "bucket: could not check whether the mapping has synced; continuing anyway.");
                return;
            }

            if (DateTimeOffset.UtcNow >= deadline)
            {
                // Not fatal. Every stat and every drop is still stored; only the join to live
                // prices is missing, and the resolve pass repairs it on the next run.
                logger.LogWarning(
                    "bucket: the mapping sync has not succeeded after {Timeout}; syncing anyway, with item IDs left for the resolve pass.",
                    MappingWaitTimeout);
                return;
            }

            logger.LogDebug("bucket: waiting for the mapping sync before resolving item IDs.");

            try
            {
                await Task.Delay(MappingPollInterval, cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return;
            }
        }
    }

    /// <summary>Reloads equipment bonuses.</summary>
    /// <param name="client">The wiki client.</param>
    /// <param name="cancellationToken">Cancels the sync.</param>
    /// <returns>Rows written.</returns>
    private async Task<int> SyncBonusesAsync(IWikiBucketClient client, CancellationToken cancellationToken)
    {
        var rows = new List<BonusesRow>();

        await foreach (var row in client.StreamAsync<BucketBonuses>(
            () => new BucketQuery("infobox_bonuses").Select(
                "page_name", "equipment_slot", "combat_style", "weapon_attack_speed", "weapon_attack_range",
                "stab_attack_bonus", "slash_attack_bonus", "crush_attack_bonus", "range_attack_bonus", "magic_attack_bonus",
                "stab_defence_bonus", "slash_defence_bonus", "crush_defence_bonus", "range_defence_bonus", "magic_defence_bonus",
                "strength_bonus", "ranged_strength_bonus", "prayer_bonus", "magic_damage_bonus"),
            orderBy: "page_name",
            PageSize,
            cancellationToken).ConfigureAwait(false))
        {
            if (string.IsNullOrWhiteSpace(row.PageName)) continue;

            rows.Add(new BonusesRow(
                row.PageName,
                row.EquipmentSlot,
                row.CombatStyle,
                row.WeaponAttackSpeed,
                row.WeaponAttackRange,
                row.StabAttack,
                row.SlashAttack,
                row.CrushAttack,
                row.RangeAttack,
                row.MagicAttack,
                row.StabDefence,
                row.SlashDefence,
                row.CrushDefence,
                row.RangeDefence,
                row.MagicDefence,
                row.StrengthBonus,
                row.RangedStrengthBonus,
                row.PrayerBonus,
                row.MagicDamageBonus));
        }

        var written = await wiki.ReplaceBonusesAsync(rows, cancellationToken).ConfigureAwait(false);
        logger.LogInformation("bucket: {Rows} equipment bonus rows.", written);
        return written;
    }

    /// <summary>Reloads monsters.</summary>
    /// <param name="client">The wiki client.</param>
    /// <param name="cancellationToken">Cancels the sync.</param>
    /// <returns>Rows written.</returns>
    private async Task<int> SyncMonstersAsync(IWikiBucketClient client, CancellationToken cancellationToken)
    {
        var rows = new List<MonsterRow>();

        await foreach (var row in client.StreamAsync<BucketMonster>(
            () => new BucketQuery("infobox_monster").Select(
                "page_name", "name", "version_anchor", "combat_level", "hitpoints",
                "slayer_level", "slayer_experience", "is_members_only"),
            orderBy: "page_name",
            PageSize,
            cancellationToken).ConfigureAwait(false))
        {
            if (string.IsNullOrWhiteSpace(row.PageName)) continue;

            rows.Add(new MonsterRow(
                row.PageName,
                row.Name,
                row.VersionAnchor,
                row.CombatLevel,
                row.Hitpoints,
                row.SlayerLevel,
                row.SlayerExperience,
                BucketFlags.IsSet(row.IsMembersOnly)));
        }

        var written = await wiki.ReplaceMonstersAsync(rows, cancellationToken).ConfigureAwait(false);
        logger.LogInformation("bucket: {Rows} monster rows.", written);
        return written;
    }

    /// <summary>Reloads drop tables, decoding the embedded per-drop JSON.</summary>
    /// <param name="client">The wiki client.</param>
    /// <param name="cancellationToken">Cancels the sync.</param>
    /// <returns>Rows written.</returns>
    private async Task<int> SyncDropsAsync(IWikiBucketClient client, CancellationToken cancellationToken)
    {
        var rows = new List<DropRow>();
        var undecodable = 0;
        var qualitativeRarity = 0;

        await foreach (var row in client.StreamAsync<BucketDrop>(
            () => new BucketQuery("dropsline").Select("item_name", "drop_json", "rare_drop_table"),
            orderBy: "item_name",
            PageSize,
            cancellationToken).ConfigureAwait(false))
        {
            if (string.IsNullOrWhiteSpace(row.DropJson)) continue;

            DropDetail? detail;
            try
            {
                detail = JsonSerializer.Deserialize(row.DropJson, GielinomicsJsonContext.Default.DropDetail);
            }
            catch (JsonException)
            {
                // One malformed drop must not fail the sync. Counted and reported rather than
                // swallowed, so a change in the embedded shape shows up as a number.
                undecodable++;
                continue;
            }

            if (detail?.DroppedFrom is not { Length: > 0 } droppedFrom) continue;

            // 'Abyssal demon#Standard' — the part after the hash is the variant of the source.
            var hash = droppedFrom.IndexOf('#', StringComparison.Ordinal);
            var sourceName = hash > 0 ? droppedFrom[..hash] : droppedFrom;
            var sourceVersion = hash > 0 ? droppedFrom[(hash + 1)..] : null;

            var rarity = DropRarity.Parse(detail.Rarity);
            if (rarity is null && !string.IsNullOrWhiteSpace(detail.Rarity)) qualitativeRarity++;

            rows.Add(new DropRow(
                row.ItemName,
                sourceName,
                sourceVersion,
                detail.Rarity,
                rarity,
                detail.QuantityLow,
                detail.QuantityHigh,
                detail.Rolls,
                detail.DropType,
                BucketFlags.IsSet(row.RareDropTable)));
        }

        var written = await wiki.ReplaceDropsAsync(rows, cancellationToken).ConfigureAwait(false);

        logger.LogInformation(
            "bucket: {Rows} drop rows; {Qualitative} carry a rarity with no numeric meaning and are stored without a probability.",
            written,
            qualitativeRarity);

        if (undecodable > 0)
        {
            logger.LogError(
                "bucket: {Count} drop rows had a drop_json that would not decode. The embedded shape may have changed.",
                undecodable);
        }

        return written;
    }
}
