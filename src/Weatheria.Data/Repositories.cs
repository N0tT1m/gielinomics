using Npgsql;

namespace Weatheria.Data;

/// <summary>A price bar ready to persist, at a known granularity.</summary>
/// <param name="ItemId">Item game ID.</param>
/// <param name="StepSeconds">Window width, as reported by the server.</param>
/// <param name="BucketTs">Window start.</param>
/// <param name="AvgHigh">Volume-weighted average instant-buy.</param>
/// <param name="AvgLow">Volume-weighted average instant-sell.</param>
/// <param name="HighVolume">Instant-buy volume.</param>
/// <param name="LowVolume">Instant-sell volume.</param>
/// <param name="Source">Which route produced this row.</param>
public readonly record struct PriceSeriesRow(
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
    /// <summary>The poll completed and its rows were persisted.</summary>
    public const string Ok = "ok";

    /// <summary>The upstream call failed.</summary>
    public const string HttpError = "http_error";

    /// <summary>The response arrived but could not be deserialised.</summary>
    public const string ParseError = "parse_error";

    /// <summary>The rows could not be written.</summary>
    public const string DbError = "db_error";
}

/// <summary>Writes and reads price history.</summary>
/// <param name="dataSource">The Postgres data source.</param>
public sealed class PriceRepository(NpgsqlDataSource dataSource)
{
    private readonly NpgsqlDataSource _dataSource = dataSource;

    /// <summary>Upserts price bars. Must be idempotent so gap repair is safe to re-run.</summary>
    /// <param name="rows">Rows to write.</param>
    /// <param name="cancellationToken">Cancels the write.</param>
    /// <returns>Rows affected.</returns>
    public Task<int> UpsertSeriesAsync(IReadOnlyCollection<PriceSeriesRow> rows, CancellationToken cancellationToken = default)
        => throw new NotImplementedException("TODO: INSERT ... ON CONFLICT (item_id, step_seconds, bucket_ts) DO UPDATE");

    /// <summary>Appends latest-trade observations. Callers filter to changed rows first.</summary>
    /// <param name="rows">Rows to write.</param>
    /// <param name="cancellationToken">Cancels the write.</param>
    /// <returns>Rows affected.</returns>
    public Task<int> InsertLatestAsync(IReadOnlyCollection<PriceLatestRow> rows, CancellationToken cancellationToken = default)
        => throw new NotImplementedException("TODO: INSERT ... ON CONFLICT DO NOTHING");

    /// <summary>Creates stub rows for item IDs seen in a feed but not yet in <c>items</c>.</summary>
    /// <remarks>This replaces the foreign key the hypertables deliberately do not have.</remarks>
    /// <param name="itemIds">IDs observed in the feed.</param>
    /// <param name="cancellationToken">Cancels the write.</param>
    /// <returns>Rows affected.</returns>
    public Task<int> EnsureItemsExistAsync(IReadOnlyCollection<int> itemIds, CancellationToken cancellationToken = default)
        => throw new NotImplementedException("TODO: INSERT INTO items (id, is_stub) ... ON CONFLICT DO UPDATE SET last_seen");

    /// <summary>Window starts already persisted at a granularity, for gap diffing.</summary>
    /// <param name="stepSeconds">Granularity to inspect.</param>
    /// <param name="from">Inclusive lower bound.</param>
    /// <param name="to">Inclusive upper bound.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The distinct window starts present.</returns>
    public Task<IReadOnlySet<DateTimeOffset>> GetPresentBucketsAsync(
        int stepSeconds,
        DateTimeOffset from,
        DateTimeOffset to,
        CancellationToken cancellationToken = default)
        => throw new NotImplementedException("TODO: SELECT DISTINCT bucket_ts WHERE step_seconds = @s AND bucket_ts BETWEEN ...");
}

/// <summary>Reads and writes item reference data.</summary>
/// <param name="dataSource">The Postgres data source.</param>
public sealed class ItemRepository(NpgsqlDataSource dataSource)
{
    private readonly NpgsqlDataSource _dataSource = dataSource;

    /// <summary>Upserts the item mapping, promoting stub rows to real ones.</summary>
    /// <param name="rows">The mapping.</param>
    /// <param name="cancellationToken">Cancels the write.</param>
    /// <returns>Rows affected.</returns>
    public Task<int> UpsertAsync(IReadOnlyCollection<ItemRow> rows, CancellationToken cancellationToken = default)
        => throw new NotImplementedException("TODO: upsert, set is_stub = false");

    /// <summary>Every known item ID, for backfill planning.</summary>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>All item IDs.</returns>
    public Task<IReadOnlyList<int>> GetAllIdsAsync(CancellationToken cancellationToken = default)
        => throw new NotImplementedException("TODO: SELECT id FROM items");
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
    private readonly NpgsqlDataSource _dataSource = dataSource;

    /// <summary>Opens a run record before the attempt starts.</summary>
    /// <param name="source">Which feed is being polled.</param>
    /// <param name="targetBucket">The window being fetched, when the feed has one.</param>
    /// <param name="cancellationToken">Cancels the write.</param>
    /// <returns>The run ID.</returns>
    public Task<long> BeginAsync(string source, DateTimeOffset? targetBucket, CancellationToken cancellationToken = default)
        => throw new NotImplementedException("TODO: INSERT INTO ingest_runs ... RETURNING id");

    /// <summary>Closes a run record with its outcome.</summary>
    /// <param name="runId">The ID from <see cref="BeginAsync"/>.</param>
    /// <param name="outcome">One of the <see cref="IngestOutcome"/> values.</param>
    /// <param name="rowsWritten">Rows persisted, when known.</param>
    /// <param name="detail">Error text on failure.</param>
    /// <param name="cancellationToken">Cancels the write.</param>
    public Task CompleteAsync(
        long runId,
        string outcome,
        int? rowsWritten = null,
        string? detail = null,
        CancellationToken cancellationToken = default)
        => throw new NotImplementedException("TODO: UPDATE ingest_runs SET outcome, rows_written, completed_at");

    /// <summary>How long since a feed last completed successfully. Drives the staleness alarm.</summary>
    /// <param name="source">The feed to check.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>Age of the last success, or null if it has never succeeded.</returns>
    public Task<TimeSpan?> TimeSinceLastSuccessAsync(string source, CancellationToken cancellationToken = default)
        => throw new NotImplementedException("TODO: SELECT max(completed_at) WHERE outcome = 'ok'");
}
