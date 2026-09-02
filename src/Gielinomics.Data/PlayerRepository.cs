using System.Text.RegularExpressions;
using Dapper;
using Npgsql;
using NpgsqlTypes;

namespace Gielinomics.Data;

/// <summary>
/// Tracked accounts, their name history, and their hiscore standings.
/// </summary>
/// <remarks>
/// Every name-keyed lookup resolves through <c>player_names</c> to a stable internal ID. A
/// rename must not 404, and must not split one account's timeline into two.
/// </remarks>
/// <param name="dataSource">The Postgres data source.</param>
public sealed partial class PlayerRepository(NpgsqlDataSource dataSource)
{
    // Two projections of the same columns. SELECTs join players against player_names and so
    // need the alias; RETURNING has no FROM clause and no alias to qualify against.
    private const string PlayerColumns = """
        p.id              AS "Id",
        p.display_name    AS "DisplayName",
        p.normalised_name AS "NormalisedName",
        p.account_type    AS "AccountType",
        p.tracked         AS "Tracked",
        p.added_at        AS "AddedAt"
        """;

    private const string PlayerReturningColumns = """
        id              AS "Id",
        display_name    AS "DisplayName",
        normalised_name AS "NormalisedName",
        account_type    AS "AccountType",
        tracked         AS "Tracked",
        added_at        AS "AddedAt"
        """;

    // Resolved through player_names, not players.normalised_name: a renamed account still has
    // its old name in the history table, and looking it up must reach the same player.
    private const string ResolveSql = $"""
        SELECT {PlayerColumns}
        FROM player_names n
        JOIN players p ON p.id = n.player_id
        WHERE n.normalised = @normalised
        ORDER BY n.seen_to IS NULL DESC, n.seen_from DESC
        LIMIT 1
        """;

    private const string InsertPlayerSql = $"""
        INSERT INTO players (display_name, normalised_name, account_type)
        VALUES (@displayName, @normalised, @accountType)
        ON CONFLICT (normalised_name) DO UPDATE SET tracked = true
        RETURNING {PlayerReturningColumns}
        """;

    // captured_at is deliberately absent from the conflict target: it is unique by
    // construction on every poll, so including it would mean the dedup never fires and the
    // table grows by a full snapshot an hour whether or not anything changed.
    private const string InsertSnapshotSql = """
        INSERT INTO hiscore_snapshots (player_id, captured_at, last_seen_at, payload, content_hash, mapping_version)
        VALUES (@playerId, @capturedAt, @capturedAt, @payload::jsonb, @contentHash, @mappingVersion)
        ON CONFLICT (player_id, content_hash) DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at
        RETURNING (xmax = 0) AS inserted, captured_at
        """;

    // Guarded by NOT EXISTS rather than ON CONFLICT: seen_from defaults to now(), so it is
    // distinct on every call and the primary key can never conflict. Without this guard,
    // re-tracking an account under different casing appends a second "current" name row.
    private const string InsertNameSql = """
        INSERT INTO player_names (player_id, name, normalised)
        SELECT @playerId, @displayName, @normalised
        WHERE NOT EXISTS (
            SELECT 1 FROM player_names
            WHERE player_id = @playerId AND normalised = @normalised AND seen_to IS NULL
        )
        """;

    private const string InsertSamplesSql = """
        INSERT INTO skill_samples (player_id, captured_at, skill, rank, level, xp)
        SELECT @playerId, @capturedAt, * FROM unnest(@skills, @ranks, @levels, @xp)
        ON CONFLICT (player_id, captured_at, skill) DO NOTHING
        """;

    private const string GainsSql = """
        SELECT skill                                              AS "Skill",
               (array_agg(xp ORDER BY captured_at ASC))[1]        AS "StartXp",
               (array_agg(xp ORDER BY captured_at DESC))[1]       AS "EndXp",
               (array_agg(level ORDER BY captured_at ASC))[1]     AS "StartLevel",
               (array_agg(level ORDER BY captured_at DESC))[1]    AS "EndLevel"
        FROM skill_samples
        WHERE player_id = @playerId AND captured_at >= @from
        GROUP BY skill
        ORDER BY skill
        """;

    private readonly NpgsqlDataSource _dataSource = dataSource;

    /// <summary>
    /// Normalises a display name for lookup.
    /// </summary>
    /// <remarks>
    /// Names are case-insensitive and may contain spaces; the game and various tools use
    /// underscores and non-breaking spaces interchangeably with them. Collapsing all of that
    /// to one form is what makes "Lynx_Titan" and "lynx  titan" the same account.
    /// </remarks>
    /// <param name="name">The display name.</param>
    /// <returns>The normalised form.</returns>
    /// <exception cref="ArgumentException"><paramref name="name"/> is null or blank.</exception>
    public static string Normalise(string name)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(name);

        // '\u00A0' spelled out rather than pasted: a literal non-breaking space in source is
        // invisible, and an editor normalising it away would silently change lookup behaviour.
        return WhitespaceRun().Replace(name.Replace('_', ' ').Replace('\u00A0', ' '), " ").Trim().ToLowerInvariant();
    }

    /// <summary>Finds a tracked account by any name it has ever had.</summary>
    /// <param name="name">The display name.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The account, or null when unknown.</returns>
    public async Task<Player?> ResolveAsync(string name, CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            return await connection.QuerySingleOrDefaultAsync<Player>(new CommandDefinition(
                ResolveSql,
                new { normalised = Normalise(name) },
                cancellationToken: cancellationToken)).ConfigureAwait(false);
        }
    }

    /// <summary>
    /// Starts tracking an account, or re-enables one already known.
    /// </summary>
    /// <param name="displayName">The display name.</param>
    /// <param name="accountType">Inferred hiscore table.</param>
    /// <param name="cancellationToken">Cancels the write.</param>
    /// <returns>The tracked account.</returns>
    public async Task<Player> TrackAsync(string displayName, string accountType, CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(displayName);
        ArgumentException.ThrowIfNullOrWhiteSpace(accountType);

        var normalised = Normalise(displayName);

        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var transaction = await connection.BeginTransactionAsync(cancellationToken).ConfigureAwait(false);
            await using (transaction.ConfigureAwait(false))
            {
                var player = await connection.QuerySingleAsync<Player>(new CommandDefinition(
                    InsertPlayerSql,
                    new { displayName, normalised, accountType },
                    transaction,
                    cancellationToken: cancellationToken)).ConfigureAwait(false);

                await connection.ExecuteAsync(new CommandDefinition(
                    InsertNameSql,
                    new { playerId = player.Id, displayName, normalised },
                    transaction,
                    cancellationToken: cancellationToken)).ConfigureAwait(false);

                await transaction.CommitAsync(cancellationToken).ConfigureAwait(false);
                return player;
            }
        }
    }

    /// <summary>Accounts the poller should visit.</summary>
    /// <remarks>An allowlist, not a crawl. Nothing is polled that somebody did not ask for.</remarks>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The tracked accounts, oldest first.</returns>
    public async Task<IReadOnlyList<Player>> ListTrackedAsync(CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var rows = await connection.QueryAsync<Player>(new CommandDefinition(
                $"SELECT {PlayerColumns} FROM players p WHERE p.tracked ORDER BY p.id",
                cancellationToken: cancellationToken)).ConfigureAwait(false);

            return [.. rows];
        }
    }

    /// <summary>
    /// Records that an account is currently using a name, closing any previous one.
    /// </summary>
    /// <param name="playerId">The account.</param>
    /// <param name="displayName">The name the API echoed back.</param>
    /// <param name="cancellationToken">Cancels the write.</param>
    /// <returns>True when this was a name we had not seen as current before.</returns>
    public async Task<bool> ObserveNameAsync(long playerId, string displayName, CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(displayName);

        var normalised = Normalise(displayName);

        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var transaction = await connection.BeginTransactionAsync(cancellationToken).ConfigureAwait(false);
            await using (transaction.ConfigureAwait(false))
            {
                var alreadyCurrent = await connection.ExecuteScalarAsync<bool>(new CommandDefinition(
                    "SELECT EXISTS (SELECT 1 FROM player_names WHERE player_id = @playerId AND normalised = @normalised AND seen_to IS NULL)",
                    new { playerId, normalised },
                    transaction,
                    cancellationToken: cancellationToken)).ConfigureAwait(false);

                if (alreadyCurrent)
                {
                    await transaction.CommitAsync(cancellationToken).ConfigureAwait(false);
                    return false;
                }

                // Close the outgoing name rather than deleting it. The old name is how a
                // historical reference to this account still resolves.
                await connection.ExecuteAsync(new CommandDefinition(
                    "UPDATE player_names SET seen_to = now() WHERE player_id = @playerId AND seen_to IS NULL",
                    new { playerId },
                    transaction,
                    cancellationToken: cancellationToken)).ConfigureAwait(false);

                await connection.ExecuteAsync(new CommandDefinition(
                    InsertNameSql,
                    new { playerId, displayName, normalised },
                    transaction,
                    cancellationToken: cancellationToken)).ConfigureAwait(false);

                await connection.ExecuteAsync(new CommandDefinition(
                    "UPDATE players SET display_name = @displayName, normalised_name = @normalised WHERE id = @playerId",
                    new { playerId, displayName, normalised },
                    transaction,
                    cancellationToken: cancellationToken)).ConfigureAwait(false);

                await transaction.CommitAsync(cancellationToken).ConfigureAwait(false);
                return true;
            }
        }
    }

    /// <summary>Every name an account has been known by.</summary>
    /// <param name="playerId">The account.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The name history, newest first.</returns>
    public async Task<IReadOnlyList<PlayerName>> GetNamesAsync(long playerId, CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var rows = await connection.QueryAsync<PlayerName>(new CommandDefinition(
                """
                SELECT name AS "Name", seen_from AS "SeenFrom", seen_to AS "SeenTo"
                FROM player_names WHERE player_id = @playerId ORDER BY seen_from DESC
                """,
                new { playerId },
                cancellationToken: cancellationToken)).ConfigureAwait(false);

            return [.. rows];
        }
    }

    /// <summary>
    /// Stores a hiscore standing, deduplicated by content.
    /// </summary>
    /// <remarks>
    /// An account that has not played since the last poll produces a byte-identical payload.
    /// Storing it again would add a row an hour per account forever and tell nobody anything;
    /// instead the existing row's <c>last_seen_at</c> moves forward.
    /// </remarks>
    /// <param name="playerId">The account.</param>
    /// <param name="capturedAt">When this poll ran.</param>
    /// <param name="payloadJson">The raw response, kept so a future mapping can re-decode it.</param>
    /// <param name="contentHash">Hash of the payload.</param>
    /// <param name="mappingVersion">Which index-to-name mapping decoded this payload.</param>
    /// <param name="samples">Per-skill standings to record alongside a genuinely new snapshot.</param>
    /// <param name="cancellationToken">Cancels the write.</param>
    /// <returns>True when the standing had actually changed.</returns>
    public async Task<bool> RecordSnapshotAsync(
        long playerId,
        DateTimeOffset capturedAt,
        string payloadJson,
        byte[] contentHash,
        int mappingVersion,
        IReadOnlyCollection<SkillSample> samples,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(payloadJson);
        ArgumentNullException.ThrowIfNull(contentHash);
        ArgumentNullException.ThrowIfNull(samples);

        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var transaction = await connection.BeginTransactionAsync(cancellationToken).ConfigureAwait(false);
            await using (transaction.ConfigureAwait(false))
            {
                // xmax = 0 is true only for a row this statement inserted, so it separates a
                // genuine change from a no-op touch without a second round trip.
                var inserted = await connection.ExecuteScalarAsync<bool>(new CommandDefinition(
                    InsertSnapshotSql,
                    new
                    {
                        playerId,
                        capturedAt = capturedAt.UtcDateTime,
                        payload = payloadJson,
                        contentHash,
                        mappingVersion,
                    },
                    transaction,
                    cancellationToken: cancellationToken)).ConfigureAwait(false);

                if (inserted && samples.Count > 0)
                {
                    await InsertSamplesAsync(connection, transaction, playerId, capturedAt, samples, cancellationToken)
                        .ConfigureAwait(false);
                }

                await transaction.CommitAsync(cancellationToken).ConfigureAwait(false);
                return inserted;
            }
        }
    }

    /// <summary>Per-skill history for an account.</summary>
    /// <param name="playerId">The account.</param>
    /// <param name="skill">A single skill index, or null for all of them.</param>
    /// <param name="from">Inclusive lower bound.</param>
    /// <param name="limit">Maximum rows.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The samples, oldest first.</returns>
    public async Task<IReadOnlyList<SkillSample>> GetHistoryAsync(
        long playerId,
        short? skill,
        DateTimeOffset from,
        int limit,
        CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var rows = await connection.QueryAsync<SkillSample>(new CommandDefinition(
                """
                SELECT captured_at AS "CapturedAt", skill AS "Skill", rank AS "Rank", level AS "Level", xp AS "Xp"
                FROM skill_samples
                WHERE player_id = @playerId AND captured_at >= @from AND (@skill IS NULL OR skill = @skill)
                ORDER BY captured_at, skill
                LIMIT @limit
                """,
                new { playerId, skill, from = from.UtcDateTime, limit },
                cancellationToken: cancellationToken)).ConfigureAwait(false);

            return [.. rows];
        }
    }

    /// <summary>Experience and levels gained over a window.</summary>
    /// <param name="playerId">The account.</param>
    /// <param name="from">Start of the window.</param>
    /// <param name="skillNames">Index-to-name mapping used to label the result.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>One row per skill with any samples in the window.</returns>
    public async Task<IReadOnlyList<SkillGain>> GetGainsAsync(
        long playerId,
        DateTimeOffset from,
        IReadOnlyList<string> skillNames,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(skillNames);

        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var rows = await connection.QueryAsync<GainRow>(new CommandDefinition(
                GainsSql,
                new { playerId, from = from.UtcDateTime },
                cancellationToken: cancellationToken)).ConfigureAwait(false);

            var gains = new List<SkillGain>();
            foreach (var row in rows)
            {
                gains.Add(new SkillGain(
                    row.Skill,
                    row.Skill >= 0 && row.Skill < skillNames.Count ? skillNames[row.Skill] : null,
                    row.StartXp,
                    row.EndXp,
                    // Unranked reads as null, and null minus null is zero movement, not a loss.
                    (row.EndXp ?? 0) - (row.StartXp ?? 0),
                    row.StartLevel,
                    row.EndLevel,
                    (row.EndLevel ?? 0) - (row.StartLevel ?? 0)));
            }

            return gains;
        }
    }

    /// <summary>Writes the per-skill rows for a new snapshot.</summary>
    /// <param name="connection">The open connection.</param>
    /// <param name="transaction">The enclosing transaction.</param>
    /// <param name="playerId">The account.</param>
    /// <param name="capturedAt">Capture time.</param>
    /// <param name="samples">The rows.</param>
    /// <param name="cancellationToken">Cancels the write.</param>
    private static async Task InsertSamplesAsync(
        NpgsqlConnection connection,
        System.Data.Common.DbTransaction transaction,
        long playerId,
        DateTimeOffset capturedAt,
        IReadOnlyCollection<SkillSample> samples,
        CancellationToken cancellationToken)
    {
        var count = samples.Count;
        var skills = new short[count];
        var ranks = new int?[count];
        var levels = new short?[count];
        var xp = new long?[count];

        var i = 0;
        foreach (var sample in samples)
        {
            skills[i] = sample.Skill;
            ranks[i] = sample.Rank;
            levels[i] = sample.Level;
            xp[i] = sample.Xp;
            i++;
        }

        var command = connection.CreateCommand();
        await using (command.ConfigureAwait(false))
        {
            command.CommandText = InsertSamplesSql;
            command.Transaction = (NpgsqlTransaction)transaction;
            command.Parameters.Add(new NpgsqlParameter<long>("playerId", playerId));
            command.Parameters.Add(new NpgsqlParameter<DateTime>("capturedAt", capturedAt.UtcDateTime));
            PriceRepository.AddArray(command, "skills", NpgsqlDbType.Smallint, skills);
            PriceRepository.AddArray(command, "ranks", NpgsqlDbType.Integer, ranks);
            PriceRepository.AddArray(command, "levels", NpgsqlDbType.Smallint, levels);
            PriceRepository.AddArray(command, "xp", NpgsqlDbType.Bigint, xp);

            await command.ExecuteNonQueryAsync(cancellationToken).ConfigureAwait(false);
        }
    }

    /// <summary>Collapses runs of whitespace to a single space.</summary>
    /// <returns>The compiled expression.</returns>
    [GeneratedRegex(@"\s+")]
    private static partial Regex WhitespaceRun();

    /// <summary>Flat projection of the gains query.</summary>
    /// <param name="Skill">Skill index.</param>
    /// <param name="StartXp">Experience at the start of the window.</param>
    /// <param name="EndXp">Experience at the end.</param>
    /// <param name="StartLevel">Level at the start.</param>
    /// <param name="EndLevel">Level at the end.</param>
    private sealed record GainRow(short Skill, long? StartXp, long? EndXp, short? StartLevel, short? EndLevel);
}
