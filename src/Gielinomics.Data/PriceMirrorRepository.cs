using Dapper;
using Npgsql;

namespace Gielinomics.Data;

/// <summary>
/// Whole-market reads shaped like the upstream real-time prices API.
/// </summary>
/// <remarks>
/// <para>
/// Every other query here is shaped for this platform's own consumers. These three are shaped
/// for somebody else's contract on purpose: <c>/mapping</c>, <c>/latest</c> and <c>/24h</c> are
/// what a client written against the wiki's prices API already asks for, and serving that shape
/// lets such a client repoint at this platform by changing a base URL. The AI service in
/// <c>src/Gielinomics.Ai</c> is the first caller, and it overrides exactly one method to do it.
/// </para>
/// <para>
/// The alternative was translating on the Python side, which would have meant reimplementing
/// item-name resolution, spread sanity and liquidity banding in a second language against a
/// second set of field names. The shape is the cheaper thing to preserve.
/// </para>
/// </remarks>
/// <param name="dataSource">The Postgres data source.</param>
public sealed class PriceMirrorRepository(NpgsqlDataSource dataSource)
{
    /// <summary>
    /// How far back to look for an item's most recent trade.
    /// </summary>
    /// <remarks>
    /// Upstream reports the last trade however old it is. Reproducing that means scanning every
    /// chunk of the hypertable on a request that runs once a minute per client, so this bounds
    /// it instead: an item nobody has traded in a month is absent rather than stale. Callers
    /// treat absent and unpriced identically — <c>Price.estimate</c> already falls through to
    /// the window averages — so the bound costs a caller nothing it was going to use.
    /// </remarks>
    public static readonly TimeSpan LatestLookback = TimeSpan.FromDays(30);

    private const string MappingSql = """
        SELECT id        AS "Id",
               name      AS "Name",
               examine   AS "Examine",
               COALESCE(members, false) AS "Members",
               buy_limit AS "Limit",
               highalch  AS "HighAlch",
               lowalch   AS "LowAlch",
               value     AS "Value",
               icon      AS "Icon"
        FROM items
        WHERE name IS NOT NULL
          AND NOT is_stub
        ORDER BY id
        """;

    // DISTINCT ON is the one Postgres extension worth reaching for here: the alternative is a
    // window function over the same rows, which materialises a rank for every row only to keep
    // the first of each group.
    private const string LatestSql = """
        SELECT DISTINCT ON (item_id)
               item_id   AS "ItemId",
               high      AS "High",
               high_time AS "HighTime",
               low       AS "Low",
               low_time  AS "LowTime"
        FROM price_latest
        WHERE observed_at >= @since
        ORDER BY item_id, observed_at DESC
        """;

    // Volume-weighted, not a mean of means. An hour that traded four units and an hour that
    // traded forty thousand are not the same evidence, and averaging their averages says they
    // are. NULLIF guards the division for an item whose bars all carry zero volume.
    private const string WindowSql = """
        SELECT item_id AS "ItemId",
               ROUND(SUM(avg_high * high_volume) / NULLIF(SUM(high_volume), 0))::BIGINT AS "AvgHighPrice",
               COALESCE(SUM(high_volume), 0)::BIGINT                                    AS "HighPriceVolume",
               ROUND(SUM(avg_low * low_volume) / NULLIF(SUM(low_volume), 0))::BIGINT    AS "AvgLowPrice",
               COALESCE(SUM(low_volume), 0)::BIGINT                                     AS "LowPriceVolume"
        FROM price_series
        WHERE step_seconds = @stepSeconds
          AND bucket_ts >= @since
        GROUP BY item_id
        """;

    private readonly NpgsqlDataSource _dataSource = dataSource;

    /// <summary>Every catalogued tradeable item.</summary>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The catalogue, by ascending item ID.</returns>
    public async Task<IReadOnlyList<MappingEntry>> GetMappingAsync(CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var rows = await connection.QueryAsync<MappingEntry>(new CommandDefinition(
                MappingSql,
                cancellationToken: cancellationToken)).ConfigureAwait(false);

            return [.. rows];
        }
    }

    /// <summary>The most recent observed trade per item.</summary>
    /// <param name="since">Oldest trade to report. Defaults to <see cref="LatestLookback"/> ago.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>One row per item that traded in the window.</returns>
    public async Task<IReadOnlyList<LatestPrice>> GetLatestAsync(
        DateTimeOffset? since = null,
        CancellationToken cancellationToken = default)
    {
        var floor = (since ?? DateTimeOffset.UtcNow - LatestLookback).UtcDateTime;

        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var rows = await connection.QueryAsync<LatestPrice>(new CommandDefinition(
                LatestSql,
                new { since = floor },
                cancellationToken: cancellationToken)).ConfigureAwait(false);

            return [.. rows];
        }
    }

    /// <summary>Volume-weighted averages per item over a trailing window.</summary>
    /// <param name="stepSeconds">Granularity of the bars to aggregate.</param>
    /// <param name="since">Start of the window.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>One row per item with a bar in the window.</returns>
    public async Task<IReadOnlyList<WindowAverage>> GetWindowAsync(
        int stepSeconds,
        DateTimeOffset since,
        CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var rows = await connection.QueryAsync<WindowAverage>(new CommandDefinition(
                WindowSql,
                new { stepSeconds, since = since.UtcDateTime },
                cancellationToken: cancellationToken)).ConfigureAwait(false);

            return [.. rows];
        }
    }
}
