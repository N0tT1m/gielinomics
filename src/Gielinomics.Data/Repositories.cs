using Npgsql;
using NpgsqlTypes;

namespace Gielinomics.Data;

/// <summary>A price bar ready to persist, at a known granularity.</summary>
/// <param name="ItemId">Item game ID.</param>
/// <param name="StepSeconds">Window width, as reported by the server.</param>
/// <param name="BucketTs">Window start.</param>
/// <param name="AvgHigh">Volume-weighted average instant-buy.</param>
/// <param name="AvgLow">Volume-weighted average instant-sell.</param>
/// <param name="HighVolume">Instant-buy volume.</param>
/// <param name="LowVolume">Instant-sell volume.</param>
/// <param name="Source">Which route produced this row.</param>
public readonly record struct
    PriceSeriesRow(
    int ItemId,
    int StepSeconds,
    DateTimeOffset BucketTs,
    decimal? AvgHigh,
    decimal? AvgLow,
    long HighVolume,
    long LowVolume,
    string Source);

/// <summary>An observed latest-trade row.</summary>
/// <param name="ItemId">Item game ID.</param>
/// <param name="ObservedAt">When we saw it.</param>
/// <param name="High">Instant-buy price.</param>
/// <param name="HighTime">When that instant-buy happened.</param>
/// <param name="Low">Instant-sell price.</param>
/// <param name="LowTime">When that instant-sell happened.</param>
public readonly record struct PriceLatestRow(
    int ItemId,
    DateTimeOffset ObservedAt,
    long? High,
    DateTimeOffset? HighTime,
    long? Low,
    DateTimeOffset? LowTime);

/// <summary>An item reference row ready to persist.</summary>
/// <param name="Id">Item game ID.</param>
/// <param name="Name">Display name.</param>
/// <param name="Examine">Examine text.</param>
/// <param name="Members">Members-only flag.</param>
/// <param name="BuyLimit">Buy limit per 4 hours, if published.</param>
/// <param name="Value">Base store value.</param>
/// <param name="LowAlch">Low alchemy value.</param>
/// <param name="HighAlch">High alchemy value.</param>
/// <param name="Icon">Wiki icon filename.</param>
public readonly record struct ItemRow(
    int Id,
    string Name,
    string? Examine,
    bool Members,
    int? BuyLimit,
    long? Value,
    long? LowAlch,
    long? HighAlch,
    string? Icon);

/// <summary>How a poll attempt ended.</summary>
public static class IngestOutcome
{
    /// <summary>
    /// The row was opened and the attempt has not reported back yet.
    /// </summary>
    /// <remarks>
    /// A run is recorded <i>before</i> the request goes out, so a worker killed mid-poll
    /// leaves evidence behind. Without a distinct opening value that row would have to
    /// claim an outcome it does not have, and a crash would be indistinguishable from a
    /// clean failure — exactly the ambiguity <c>ingest_runs</c> exists to remove.
    /// </remarks>
    public const string Running = "running";

    /// <summary>The poll completed and its rows were persisted.</summary>
    public const string Ok = "ok";

    /// <summary>The upstream call failed.</summary>
    public const string HttpError = "http_error";

    /// <summary>The response arrived but could not be deserialised.</summary>
    public const string ParseError = "parse_error";

    /// <summary>The rows could not be written.</summary>
    public const string DbError = "db_error";

    /// <summary>
    /// The attempt failed in a way the worker could not classify.
    /// </summary>
    /// <remarks>
    /// Its own value rather than being folded into the nearest specific one. An audit trail
    /// that reports a novel failure as a <c>db_error</c> is worse than one that admits it does
    /// not know: the first sends you to the database, the second sends you to the logs.
    /// </remarks>
    public const string UnknownError = "unknown_error";
}

/// <summary>Writes and reads price history.</summary>
/// <param name="dataSource">The Postgres data source.</param>
public sealed class PriceRepository(NpgsqlDataSource dataSource)
{
    // Multi-row writes go through unnest() over parallel arrays rather than a VALUES list.
    // One statement, one round trip, and a stable plan regardless of batch size: a 5m poll
    // writes ~3700 rows, and a 3700-tuple VALUES list re-plans on every distinct row count.
    private const string UpsertSeriesSql = """
        INSERT INTO price_series
            (item_id, step_seconds, bucket_ts, avg_high, avg_low, high_volume, low_volume, source)
        SELECT * FROM unnest(
            @item_ids, @step_seconds, @bucket_ts, @avg_high, @avg_low, @high_volume, @low_volume, @source)
        ON CONFLICT (item_id, step_seconds, bucket_ts) DO UPDATE SET
            avg_high    = EXCLUDED.avg_high,
            avg_low     = EXCLUDED.avg_low,
            high_volume = EXCLUDED.high_volume,
            low_volume  = EXCLUDED.low_volume,
            source      = EXCLUDED.source
        """;

    private const string InsertLatestSql = """
        INSERT INTO price_latest (item_id, observed_at, high, high_time, low, low_time)
        SELECT * FROM unnest(@item_ids, @observed_at, @high, @high_time, @low, @low_time)
        ON CONFLICT (item_id, observed_at) DO NOTHING
        """;

    private const string EnsureItemsSql = """
        INSERT INTO items (id, is_stub)
        SELECT unnest(@item_ids), true
        ON CONFLICT (id) DO UPDATE SET last_seen = now()
        """;

    private const string PresentBucketsSql = """
        SELECT DISTINCT bucket_ts
        FROM price_series
        WHERE step_seconds = @step_seconds
          AND bucket_ts >= @from_ts
          AND bucket_ts <= @to_ts
        """;

    private readonly NpgsqlDataSource _dataSource = dataSource;

    /// <summary>Upserts price bars. Must be idempotent so gap repair is safe to re-run.</summary>
    /// <param name="rows">Rows to write.</param>
    /// <param name="cancellationToken">Cancels the write.</param>
    /// <returns>Rows affected.</returns>
    /// <exception cref="ArgumentNullException"><paramref name="rows"/> is null.</exception>
    public async Task<int> UpsertSeriesAsync(IReadOnlyCollection<PriceSeriesRow> rows, CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(rows);

        if (rows.Count == 0)
        {
            return 0;
        }

        var count = rows.Count;
        var itemIds = new int[count];
        var steps = new int[count];
        var buckets = new DateTime[count];
        var avgHighs = new decimal?[count];
        var avgLows = new decimal?[count];
        var highVolumes = new long[count];
        var lowVolumes = new long[count];
        var sources = new string[count];

        var i = 0;
        foreach (var row in rows)
        {
            itemIds[i] = row.ItemId;
            steps[i] = row.StepSeconds;
            buckets[i] = row.BucketTs.UtcDateTime;
            avgHighs[i] = row.AvgHigh;
            avgLows[i] = row.AvgLow;
            highVolumes[i] = row.HighVolume;
            lowVolumes[i] = row.LowVolume;
            sources[i] = row.Source;
            i++;
        }

        var command = _dataSource.CreateCommand(UpsertSeriesSql);
        await using (command.ConfigureAwait(false))
        {
            AddArray(command, "item_ids", NpgsqlDbType.Integer, itemIds);
            AddArray(command, "step_seconds", NpgsqlDbType.Integer, steps);
            AddArray(command, "bucket_ts", NpgsqlDbType.TimestampTz, buckets);
            AddArray(command, "avg_high", NpgsqlDbType.Numeric, avgHighs);
            AddArray(command, "avg_low", NpgsqlDbType.Numeric, avgLows);
            AddArray(command, "high_volume", NpgsqlDbType.Bigint, highVolumes);
            AddArray(command, "low_volume", NpgsqlDbType.Bigint, lowVolumes);
            AddArray(command, "source", NpgsqlDbType.Text, sources);

            return await command.ExecuteNonQueryAsync(cancellationToken).ConfigureAwait(false);
        }
    }

    /// <summary>Appends latest-trade observations. Callers filter to changed rows first.</summary>
    /// <param name="rows">Rows to write.</param>
    /// <param name="cancellationToken">Cancels the write.</param>
    /// <returns>Rows affected.</returns>
    /// <exception cref="ArgumentNullException"><paramref name="rows"/> is null.</exception>
    public async Task<int> InsertLatestAsync(IReadOnlyCollection<PriceLatestRow> rows, CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(rows);

        if (rows.Count == 0)
        {
            return 0;
        }

        var count = rows.Count;
        var itemIds = new int[count];
        var observedAt = new DateTime[count];
        var highs = new long?[count];
        var highTimes = new DateTime?[count];
        var lows = new long?[count];
        var lowTimes = new DateTime?[count];

        var i = 0;
        foreach (var row in rows)
        {
            itemIds[i] = row.ItemId;
            observedAt[i] = row.ObservedAt.UtcDateTime;
            highs[i] = row.High;
            highTimes[i] = row.HighTime?.UtcDateTime;
            lows[i] = row.Low;
            lowTimes[i] = row.LowTime?.UtcDateTime;
            i++;
        }

        var command = _dataSource.CreateCommand(InsertLatestSql);
        await using (command.ConfigureAwait(false))
        {
            AddArray(command, "item_ids", NpgsqlDbType.Integer, itemIds);
            AddArray(command, "observed_at", NpgsqlDbType.TimestampTz, observedAt);
            AddArray(command, "high", NpgsqlDbType.Bigint, highs);
            AddArray(command, "high_time", NpgsqlDbType.TimestampTz, highTimes);
            AddArray(command, "low", NpgsqlDbType.Bigint, lows);
            AddArray(command, "low_time", NpgsqlDbType.TimestampTz, lowTimes);

            return await command.ExecuteNonQueryAsync(cancellationToken).ConfigureAwait(false);
        }
    }

    /// <summary>Creates stub rows for item IDs seen in a feed but not yet in <c>items</c>.</summary>
    /// <remarks>This replaces the foreign key the hypertables deliberately do not have.</remarks>
    /// <param name="itemIds">IDs observed in the feed.</param>
    /// <param name="cancellationToken">Cancels the write.</param>
    /// <returns>Rows affected.</returns>
    /// <exception cref="ArgumentNullException"><paramref name="itemIds"/> is null.</exception>
    public async Task<int> EnsureItemsExistAsync(IReadOnlyCollection<int> itemIds, CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(itemIds);

        if (itemIds.Count == 0)
        {
            return 0;
        }

        var command = _dataSource.CreateCommand(EnsureItemsSql);
        await using (command.ConfigureAwait(false))
        {
            AddArray(command, "item_ids", NpgsqlDbType.Integer, itemIds as int[] ?? [.. itemIds]);

            return await command.ExecuteNonQueryAsync(cancellationToken).ConfigureAwait(false);
        }
    }

    /// <summary>Window starts already persisted at a granularity, for gap diffing.</summary>
    /// <param name="stepSeconds">Granularity to inspect.</param>
    /// <param name="from">Inclusive lower bound.</param>
    /// <param name="to">Inclusive upper bound.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The distinct window starts present.</returns>
    public async Task<IReadOnlySet<DateTimeOffset>> GetPresentBucketsAsync(
        int stepSeconds,
        DateTimeOffset from,
        DateTimeOffset to,
        CancellationToken cancellationToken = default)
    {
        var present = new HashSet<DateTimeOffset>();

        var command = _dataSource.CreateCommand(PresentBucketsSql);
        await using (command.ConfigureAwait(false))
        {
            command.Parameters.Add(new NpgsqlParameter<int>("step_seconds", stepSeconds));
            command.Parameters.Add(new NpgsqlParameter<DateTime>("from_ts", from.UtcDateTime));
            command.Parameters.Add(new NpgsqlParameter<DateTime>("to_ts", to.UtcDateTime));

            var reader = await command.ExecuteReaderAsync(cancellationToken).ConfigureAwait(false);
            await using (reader.ConfigureAwait(false))
            {
                while (await reader.ReadAsync(cancellationToken).ConfigureAwait(false))
                {
                    present.Add(reader.GetFieldValue<DateTimeOffset>(0));
                }
            }
        }

        return present;
    }

    /// <summary>Adds a typed array parameter.</summary>
    /// <typeparam name="T">Element type.</typeparam>
    /// <param name="command">The command to add to.</param>
    /// <param name="name">Parameter name, without the sigil.</param>
    /// <param name="elementType">The element's Postgres type.</param>
    /// <param name="values">The array.</param>
    internal static void AddArray<T>(NpgsqlCommand command, string name, NpgsqlDbType elementType, T[] values)
        => command.Parameters.Add(new NpgsqlParameter(name, NpgsqlDbType.Array | elementType) { Value = values });
}

/// <summary>Reads and writes item reference data.</summary>
/// <param name="dataSource">The Postgres data source.</param>
public sealed class ItemRepository(NpgsqlDataSource dataSource)
{
    private const string UpsertSql = """
        INSERT INTO items (id, name, examine, members, buy_limit, value, lowalch, highalch, icon, is_stub)
        SELECT *, false FROM unnest(
            @ids, @names, @examines, @members, @buy_limits, @values, @lowalchs, @highalchs, @icons)
        ON CONFLICT (id) DO UPDATE SET
            name      = EXCLUDED.name,
            examine   = EXCLUDED.examine,
            members   = EXCLUDED.members,
            buy_limit = EXCLUDED.buy_limit,
            value     = EXCLUDED.value,
            lowalch   = EXCLUDED.lowalch,
            highalch  = EXCLUDED.highalch,
            icon      = EXCLUDED.icon,
            is_stub   = false,
            last_seen = now()
        """;

    private readonly NpgsqlDataSource _dataSource = dataSource;

    /// <summary>Upserts the item mapping, promoting stub rows to real ones.</summary>
    /// <param name="rows">The mapping.</param>
    /// <param name="cancellationToken">Cancels the write.</param>
    /// <returns>Rows affected.</returns>
    /// <exception cref="ArgumentNullException"><paramref name="rows"/> is null.</exception>
    public async Task<int> UpsertAsync(IReadOnlyCollection<ItemRow> rows, CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(rows);

        if (rows.Count == 0)
        {
            return 0;
        }

        var count = rows.Count;
        var ids = new int[count];
        var names = new string[count];
        var examines = new string?[count];
        var members = new bool[count];
        var buyLimits = new int?[count];
        var values = new long?[count];
        var lowAlchs = new long?[count];
        var highAlchs = new long?[count];
        var icons = new string?[count];

        var i = 0;
        foreach (var row in rows)
        {
            ids[i] = row.Id;
            names[i] = row.Name;
            examines[i] = row.Examine;
            members[i] = row.Members;
            buyLimits[i] = row.BuyLimit;
            values[i] = row.Value;
            lowAlchs[i] = row.LowAlch;
            highAlchs[i] = row.HighAlch;
            icons[i] = row.Icon;
            i++;
        }

        var command = _dataSource.CreateCommand(UpsertSql);
        await using (command.ConfigureAwait(false))
        {
            PriceRepository.AddArray(command, "ids", NpgsqlDbType.Integer, ids);
            PriceRepository.AddArray(command, "names", NpgsqlDbType.Text, names);
            PriceRepository.AddArray(command, "examines", NpgsqlDbType.Text, examines);
            PriceRepository.AddArray(command, "members", NpgsqlDbType.Boolean, members);
            PriceRepository.AddArray(command, "buy_limits", NpgsqlDbType.Integer, buyLimits);
            PriceRepository.AddArray(command, "values", NpgsqlDbType.Bigint, values);
            PriceRepository.AddArray(command, "lowalchs", NpgsqlDbType.Bigint, lowAlchs);
            PriceRepository.AddArray(command, "highalchs", NpgsqlDbType.Bigint, highAlchs);
            PriceRepository.AddArray(command, "icons", NpgsqlDbType.Text, icons);

            return await command.ExecuteNonQueryAsync(cancellationToken).ConfigureAwait(false);
        }
    }

    /// <summary>Every known item ID, for backfill planning.</summary>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>All item IDs.</returns>
    public async Task<IReadOnlyList<int>> GetAllIdsAsync(CancellationToken cancellationToken = default)
    {
        var ids = new List<int>();

        var command = _dataSource.CreateCommand("SELECT id FROM items ORDER BY id");
        await using (command.ConfigureAwait(false))
        {
            var reader = await command.ExecuteReaderAsync(cancellationToken).ConfigureAwait(false);
            await using (reader.ConfigureAwait(false))
            {
                while (await reader.ReadAsync(cancellationToken).ConfigureAwait(false))
                {
                    ids.Add(reader.GetInt32(0));
                }
            }
        }

        return ids;
    }
}

/// <summary>
/// Records every poll attempt, whether or not it produced rows.
/// </summary>
/// <remarks>
/// The only thing distinguishing a quiet market from a dead worker, and not
/// reconstructable after the fact — which is why it is Phase 1, not Phase 2.
/// </remarks>
/// <param name="dataSource">The Postgres data source.</param>
public sealed class IngestRunRepository(NpgsqlDataSource dataSource)
{
    private const string BeginSql = """
        INSERT INTO ingest_runs (source, target_bucket, outcome)
        VALUES (@source, @target_bucket, @outcome)
        RETURNING id
        """;

    private const string CompleteSql = """
        UPDATE ingest_runs
        SET outcome      = @outcome,
            rows_written = @rows_written,
            detail       = @detail,
            completed_at = now()
        WHERE id = @id
        """;

    // now() - max(...) rather than a client-side subtraction: the worker's clock and the
    // database's clock are not the same clock, and a skewed container would otherwise
    // produce a negative staleness and silence the alarm.
    private const string LastSuccessSql = """
        SELECT now() - max(completed_at)
        FROM ingest_runs
        WHERE source = @source AND outcome = 'ok' AND completed_at IS NOT NULL
        """;

    private readonly NpgsqlDataSource _dataSource = dataSource;

    /// <summary>Opens a run record before the attempt starts.</summary>
    /// <param name="source">Which feed is being polled.</param>
    /// <param name="targetBucket">The window being fetched, when the feed has one.</param>
    /// <param name="cancellationToken">Cancels the write.</param>
    /// <returns>The run ID.</returns>
    public async Task<long> BeginAsync(string source, DateTimeOffset? targetBucket, CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(source);

        var command = _dataSource.CreateCommand(BeginSql);
        await using (command.ConfigureAwait(false))
        {
            command.Parameters.Add(new NpgsqlParameter<string>("source", source));
            command.Parameters.Add(new NpgsqlParameter("target_bucket", NpgsqlDbType.TimestampTz)
            {
                Value = (object?)targetBucket?.UtcDateTime ?? DBNull.Value,
            });
            command.Parameters.Add(new NpgsqlParameter<string>("outcome", IngestOutcome.Running));

            var id = await command.ExecuteScalarAsync(cancellationToken).ConfigureAwait(false);
            return (long)id!;
        }
    }

    /// <summary>Closes a run record with its outcome.</summary>
    /// <param name="runId">The ID from <see cref="BeginAsync"/>.</param>
    /// <param name="outcome">One of the <see cref="IngestOutcome"/> values.</param>
    /// <param name="rowsWritten">Rows persisted, when known.</param>
    /// <param name="detail">Error text on failure.</param>
    /// <param name="cancellationToken">Cancels the write.</param>
    public async Task CompleteAsync(
        long runId,
        string outcome,
        int? rowsWritten = null,
        string? detail = null,
        CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(outcome);

        var command = _dataSource.CreateCommand(CompleteSql);
        await using (command.ConfigureAwait(false))
        {
            command.Parameters.Add(new NpgsqlParameter<long>("id", runId));
            command.Parameters.Add(new NpgsqlParameter<string>("outcome", outcome));
            command.Parameters.Add(new NpgsqlParameter("rows_written", NpgsqlDbType.Integer)
            {
                Value = (object?)rowsWritten ?? DBNull.Value,
            });
            command.Parameters.Add(new NpgsqlParameter("detail", NpgsqlDbType.Text)
            {
                // Truncated: detail carries exception text, and an unbounded message from a
                // failing dependency should not be able to bloat the audit table.
                Value = detail is null ? DBNull.Value : Truncate(detail, 2000),
            });

            await command.ExecuteNonQueryAsync(cancellationToken).ConfigureAwait(false);
        }
    }

    /// <summary>How long since a feed last completed successfully. Drives the staleness alarm.</summary>
    /// <param name="source">The feed to check.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>Age of the last success, or null if it has never succeeded.</returns>
    public async Task<TimeSpan?> TimeSinceLastSuccessAsync(string source, CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(source);

        var command = _dataSource.CreateCommand(LastSuccessSql);
        await using (command.ConfigureAwait(false))
        {
            command.Parameters.Add(new NpgsqlParameter<string>("source", source));

            var result = await command.ExecuteScalarAsync(cancellationToken).ConfigureAwait(false);
            return result is TimeSpan age ? age : null;
        }
    }

    /// <summary>Clips a string to a maximum length.</summary>
    /// <param name="value">The text.</param>
    /// <param name="maxLength">Maximum characters to keep.</param>
    /// <returns>The clipped text.</returns>
    private static string Truncate(string value, int maxLength)
        => value.Length <= maxLength ? value : value[..maxLength];
}
