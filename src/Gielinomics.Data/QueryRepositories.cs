using Dapper;
using Npgsql;

namespace Gielinomics.Data;

/// <summary>
/// Read-side queries over the accumulated market history.
/// </summary>
/// <remarks>
/// Column aliases are quoted PascalCase throughout. Postgres folds unquoted identifiers to
/// lower case, and Dapper's constructor binding matches on name — quoting is what keeps the
/// mapping explicit instead of dependent on a global Dapper setting.
/// </remarks>
/// <param name="dataSource">The Postgres data source.</param>
public sealed class MarketQueryRepository(NpgsqlDataSource dataSource)
{
    private const string SearchItemsSql = """
        SELECT id          AS "Id",
               name        AS "Name",
               members     AS "Members",
               buy_limit   AS "BuyLimit",
               highalch    AS "HighAlch",
               icon        AS "Icon",
               is_stub     AS "IsStub"
        FROM items
        WHERE (@search IS NULL OR name ILIKE '%' || @search || '%')
          AND (@members IS NULL OR members = @members)
          AND (@minBuyLimit IS NULL OR buy_limit >= @minBuyLimit)
          AND id > @after
        ORDER BY id
        LIMIT @limit
        """;

    private const string GetItemSql = """
        SELECT id         AS "Id",
               name       AS "Name",
               examine    AS "Examine",
               members    AS "Members",
               buy_limit  AS "BuyLimit",
               value      AS "Value",
               lowalch    AS "LowAlch",
               highalch   AS "HighAlch",
               icon       AS "Icon",
               is_stub    AS "IsStub",
               first_seen AS "FirstSeen",
               last_seen  AS "LastSeen"
        FROM items
        WHERE id = @id
        """;

    private const string GetPricesSql = """
        SELECT bucket_ts   AS "BucketTs",
               avg_high    AS "AvgHigh",
               avg_low     AS "AvgLow",
               high_volume AS "HighVolume",
               low_volume  AS "LowVolume"
        FROM price_series
        WHERE item_id = @itemId
          AND step_seconds = @stepSeconds
          AND bucket_ts >= @from
          AND bucket_ts <= @to
        ORDER BY bucket_ts
        LIMIT @limit
        """;

    // stddev_samp over a single sample is null, not zero, and that null is correct: one bar
    // says nothing about volatility. It propagates into Volatility rather than being coalesced.
    private const string GetStatsSql = """
        SELECT count(*) FILTER (WHERE avg_high IS NOT NULL)                     AS "Samples",
               avg(avg_high)                                                    AS "MeanHigh",
               stddev_samp(avg_high)                                            AS "StdDevHigh",
               stddev_samp(avg_high) / nullif(avg(avg_high), 0)                 AS "Volatility",
               avg((avg_high - avg_low) / nullif(avg_high, 0))
                   FILTER (WHERE avg_high IS NOT NULL AND avg_low IS NOT NULL)  AS "MeanSpread",
               coalesce(sum(high_volume), 0)::bigint                            AS "HighVolume",
               coalesce(sum(low_volume), 0)::bigint                             AS "LowVolume"
        FROM price_series
        WHERE item_id = @itemId AND step_seconds = @stepSeconds AND bucket_ts >= @from
        """;

    // First and last non-null price in the window, picked with array_agg + FILTER rather than
    // two correlated subqueries: one pass over the window per item instead of three.
    private const string MoversSql = """
        WITH bounds AS (
            SELECT item_id,
                   (array_agg(avg_high ORDER BY bucket_ts ASC)  FILTER (WHERE avg_high IS NOT NULL))[1] AS start_price,
                   (array_agg(avg_high ORDER BY bucket_ts DESC) FILTER (WHERE avg_high IS NOT NULL))[1] AS end_price,
                   coalesce(sum(high_volume + low_volume), 0)::bigint AS volume
            FROM price_series
            WHERE step_seconds = @stepSeconds AND bucket_ts >= @from
            GROUP BY item_id
        )
        SELECT b.item_id                                                      AS "ItemId",
               i.name                                                         AS "Name",
               b.start_price                                                  AS "StartPrice",
               b.end_price                                                    AS "EndPrice",
               ((b.end_price - b.start_price) / b.start_price * 100)          AS "ChangePercent",
               b.volume                                                       AS "Volume"
        FROM bounds b
        LEFT JOIN items i ON i.id = b.item_id
        WHERE b.start_price IS NOT NULL
          AND b.end_price IS NOT NULL
          -- A floor on BOTH ends, not just the start: a percentage move is only meaningful
          -- between two prices somebody could actually have traded at. Without it the ranking
          -- fills with items whose first recorded bar was a lone 2 gp trade.
          AND b.start_price >= @minPrice
          AND b.end_price >= @minPrice
          AND b.volume >= @minVolume
        ORDER BY abs((b.end_price - b.start_price) / b.start_price) DESC
        LIMIT @limit
        """;

    // DISTINCT ON is the cheap way to take the newest row per item out of an append-only
    // table. The observed_at floor keeps a delisted item's year-old row out of a live scan.
    private const string SpreadsSql = """
        WITH newest AS (
            SELECT DISTINCT ON (item_id) item_id, high, low, observed_at
            FROM price_latest
            WHERE observed_at >= now() - @freshness::interval
            ORDER BY item_id, observed_at DESC
        ),
        volumes AS (
            SELECT item_id,
                   coalesce(sum(high_volume), 0)::bigint AS high_volume,
                   coalesce(sum(low_volume), 0)::bigint  AS low_volume
            FROM price_series
            WHERE step_seconds = 300 AND bucket_ts >= now() - @freshness::interval
            GROUP BY item_id
        )
        SELECT n.item_id                       AS "ItemId",
               i.name                          AS "Name",
               n.high                          AS "High",
               n.low                           AS "Low",
               i.buy_limit                     AS "BuyLimit",
               coalesce(v.high_volume, 0)      AS "HighVolume",
               coalesce(v.low_volume, 0)       AS "LowVolume",
               n.observed_at                   AS "ObservedAt"
        FROM newest n
        LEFT JOIN items i   ON i.id = n.item_id
        LEFT JOIN volumes v ON v.item_id = n.item_id
        WHERE n.high IS NOT NULL
          AND n.low IS NOT NULL
          AND n.high > n.low
          AND least(coalesce(v.high_volume, 0), coalesce(v.low_volume, 0)) >= @minVolume
        ORDER BY (n.high - n.low) DESC
        LIMIT @limit
        """;

    private const string SpreadForItemSql = """
        WITH newest AS (
            SELECT item_id, high, low, observed_at
            FROM price_latest
            WHERE item_id = @itemId AND observed_at >= now() - @freshness::interval
            ORDER BY observed_at DESC
            LIMIT 1
        ),
        volumes AS (
            SELECT coalesce(sum(high_volume), 0)::bigint AS high_volume,
                   coalesce(sum(low_volume), 0)::bigint  AS low_volume
            FROM price_series
            WHERE item_id = @itemId AND step_seconds = 300 AND bucket_ts >= now() - @freshness::interval
        )
        SELECT n.item_id                  AS "ItemId",
               i.name                     AS "Name",
               n.high                     AS "High",
               n.low                      AS "Low",
               i.buy_limit                AS "BuyLimit",
               coalesce(v.high_volume, 0) AS "HighVolume",
               coalesce(v.low_volume, 0)  AS "LowVolume",
               n.observed_at              AS "ObservedAt"
        FROM newest n
        LEFT JOIN items i ON i.id = n.item_id
        CROSS JOIN volumes v
        WHERE n.high IS NOT NULL AND n.low IS NOT NULL
        """;

    private readonly NpgsqlDataSource _dataSource = dataSource;

    /// <summary>Searches items, ordered by ID for stable cursor pagination.</summary>
    /// <param name="search">Case-insensitive substring of the name, or null for no filter.</param>
    /// <param name="members">Members-only filter, or null for no filter.</param>
    /// <param name="minBuyLimit">Minimum buy limit, or null for no filter.</param>
    /// <param name="after">Exclusive lower bound on the item ID — the cursor.</param>
    /// <param name="limit">Maximum rows.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The matching page.</returns>
    public async Task<IReadOnlyList<ItemSummary>> SearchItemsAsync(
        string? search,
        bool? members,
        int? minBuyLimit,
        int after,
        int limit,
        CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var rows = await connection.QueryAsync<ItemSummary>(new CommandDefinition(
                SearchItemsSql,
                new { search, members, minBuyLimit, after, limit },
                cancellationToken: cancellationToken)).ConfigureAwait(false);

            return [.. rows];
        }
    }

    /// <summary>Fetches one item's reference metadata.</summary>
    /// <param name="id">Item game ID.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The item, or null when unknown.</returns>
    public async Task<ItemDetail?> GetItemAsync(int id, CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            return await connection.QuerySingleOrDefaultAsync<ItemDetail>(new CommandDefinition(
                GetItemSql,
                new { id },
                cancellationToken: cancellationToken)).ConfigureAwait(false);
        }
    }

    /// <summary>Fetches retained price history for one item.</summary>
    /// <param name="itemId">Item game ID.</param>
    /// <param name="stepSeconds">Granularity.</param>
    /// <param name="from">Inclusive lower bound.</param>
    /// <param name="to">Inclusive upper bound.</param>
    /// <param name="limit">Maximum bars.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The series, oldest first.</returns>
    public async Task<IReadOnlyList<PricePoint>> GetPricesAsync(
        int itemId,
        int stepSeconds,
        DateTimeOffset from,
        DateTimeOffset to,
        int limit,
        CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var rows = await connection.QueryAsync<PricePoint>(new CommandDefinition(
                GetPricesSql,
                new { itemId, stepSeconds, from = from.UtcDateTime, to = to.UtcDateTime, limit },
                cancellationToken: cancellationToken)).ConfigureAwait(false);

            return [.. rows];
        }
    }

    /// <summary>Computes derived statistics over an item's retained history.</summary>
    /// <param name="itemId">Item game ID.</param>
    /// <param name="stepSeconds">Granularity to compute over.</param>
    /// <param name="from">Start of the analysed window.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The statistics. Samples is zero when nothing is retained.</returns>
    public async Task<ItemStats> GetStatsAsync(
        int itemId,
        int stepSeconds,
        DateTimeOffset from,
        CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var row = await connection.QuerySingleAsync<StatsRow>(new CommandDefinition(
                GetStatsSql,
                new { itemId, stepSeconds, from = from.UtcDateTime },
                cancellationToken: cancellationToken)).ConfigureAwait(false);

            return new ItemStats(
                itemId,
                stepSeconds,
                from,
                row.Samples,
                row.MeanHigh,
                row.StdDevHigh,
                row.Volatility,
                row.MeanSpread,
                row.HighVolume,
                row.LowVolume);
        }
    }

    /// <summary>Ranks items by absolute percentage price change over a window.</summary>
    /// <param name="stepSeconds">Granularity to measure over.</param>
    /// <param name="from">Start of the window.</param>
    /// <param name="minVolume">Minimum total volume, to exclude illiquid noise.</param>
    /// <param name="minPrice">
    /// Minimum price at both ends of the window. A move from 2 gp to 115 gp is a true
    /// 5,650% change and completely useless: it is one trade on an item nobody trades.
    /// </param>
    /// <param name="limit">Maximum rows.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The movers, largest absolute move first.</returns>
    public async Task<IReadOnlyList<MarketMover>> GetMoversAsync(
        int stepSeconds,
        DateTimeOffset from,
        long minVolume,
        long minPrice,
        int limit,
        CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var rows = await connection.QueryAsync<MarketMover>(new CommandDefinition(
                MoversSql,
                new { stepSeconds, from = from.UtcDateTime, minVolume, minPrice, limit },
                cancellationToken: cancellationToken)).ConfigureAwait(false);

            return [.. rows];
        }
    }

    /// <summary>Fetches raw buy/sell spreads for tax adjustment upstream.</summary>
    /// <param name="minVolume">Minimum volume on <i>both</i> sides — a spread you cannot exit is not a margin.</param>
    /// <param name="freshness">How recent the latest observation must be.</param>
    /// <param name="limit">Maximum candidates.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>Candidates, widest gross spread first.</returns>
    public async Task<IReadOnlyList<SpreadCandidate>> GetSpreadsAsync(
        long minVolume,
        TimeSpan freshness,
        int limit,
        CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var rows = await connection.QueryAsync<SpreadCandidate>(new CommandDefinition(
                SpreadsSql,
                new { minVolume, freshness, limit },
                cancellationToken: cancellationToken)).ConfigureAwait(false);

            return [.. rows];
        }
    }

    /// <summary>
    /// Every known item ID with its name.
    /// </summary>
    /// <remarks>
    /// Feeds the tax exemption resolver. Stubs are excluded because a stub has no name to
    /// match against, and including them would only grow the result for nothing.
    /// </remarks>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>Named items.</returns>
    public async Task<IReadOnlyList<ItemName>> GetItemNamesAsync(CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var rows = await connection.QueryAsync<ItemName>(new CommandDefinition(
                """SELECT id AS "Id", name AS "Name" FROM items WHERE name IS NOT NULL""",
                cancellationToken: cancellationToken)).ConfigureAwait(false);

            return [.. rows];
        }
    }

    /// <summary>Fetches the newest spread for a single item.</summary>
    /// <param name="itemId">Item game ID.</param>
    /// <param name="freshness">How recent the observation must be.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The candidate, or null when nothing fresh enough is stored.</returns>
    public async Task<SpreadCandidate?> GetSpreadForItemAsync(
        int itemId,
        TimeSpan freshness,
        CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            return await connection.QuerySingleOrDefaultAsync<SpreadCandidate>(new CommandDefinition(
                SpreadForItemSql,
                new { itemId, freshness },
                cancellationToken: cancellationToken)).ConfigureAwait(false);
        }
    }

    /// <summary>Flat projection of the statistics query, before the item ID is folded back in.</summary>
    /// <param name="Samples">Bars with a price.</param>
    /// <param name="MeanHigh">Mean instant-buy.</param>
    /// <param name="StdDevHigh">Standard deviation of instant-buy.</param>
    /// <param name="Volatility">Coefficient of variation.</param>
    /// <param name="MeanSpread">Mean relative spread.</param>
    /// <param name="HighVolume">Total instant-buy volume.</param>
    /// <param name="LowVolume">Total instant-sell volume.</param>
    private sealed record StatsRow(
        long Samples,
        decimal? MeanHigh,
        decimal? StdDevHigh,
        decimal? Volatility,
        decimal? MeanSpread,
        long HighVolume,
        long LowVolume);
}

/// <summary>Reads the ingest audit trail.</summary>
/// <param name="dataSource">The Postgres data source.</param>
public sealed class IngestQueryRepository(NpgsqlDataSource dataSource)
{
    // 'running' is excluded from the failure count on purpose: a run that is merely still
    // open is not a failure, and counting it as one would make every healthy poll look bad
    // for the few hundred milliseconds it is in flight.
    private const string StatusSql = """
        SELECT source AS "Source",
               max(completed_at) FILTER (WHERE outcome = 'ok')                       AS "LastSuccessAt",
               now() - max(completed_at) FILTER (WHERE outcome = 'ok')               AS "SinceLastSuccess",
               count(*) FILTER (WHERE attempted_at >= now() - interval '1 day')      AS "RunsLastDay",
               count(*) FILTER (WHERE attempted_at >= now() - interval '1 day'
                                  AND outcome NOT IN ('ok', 'running'))              AS "FailuresLastDay"
        FROM ingest_runs
        GROUP BY source
        ORDER BY source
        """;

    private const string CoverageSql = """
        SELECT count(DISTINCT bucket_ts)
        FROM price_series
        WHERE step_seconds = @stepSeconds AND bucket_ts >= @from AND bucket_ts <= @to
        """;

    private readonly NpgsqlDataSource _dataSource = dataSource;

    /// <summary>Per-feed health, one row per source that has ever run.</summary>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>Feed statuses.</returns>
    public async Task<IReadOnlyList<FeedStatus>> GetStatusAsync(CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var rows = await connection.QueryAsync<FeedStatus>(new CommandDefinition(
                StatusSql,
                cancellationToken: cancellationToken)).ConfigureAwait(false);

            return [.. rows];
        }
    }

    /// <summary>
    /// Fraction of the expected windows in a span that are actually stored.
    /// </summary>
    /// <param name="stepSeconds">Granularity to inspect.</param>
    /// <param name="from">Start of the span.</param>
    /// <param name="to">End of the span.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The coverage report.</returns>
    public async Task<CoverageReport> GetCoverageAsync(
        int stepSeconds,
        DateTimeOffset from,
        DateTimeOffset to,
        CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var present = await connection.ExecuteScalarAsync<long>(new CommandDefinition(
                CoverageSql,
                new { stepSeconds, from = from.UtcDateTime, to = to.UtcDateTime },
                cancellationToken: cancellationToken)).ConfigureAwait(false);

            var span = to - from;
            var expected = span <= TimeSpan.Zero ? 0L : (long)(span.TotalSeconds / stepSeconds) + 1;

            // Clamped: an inclusive range can hold one more boundary than the arithmetic
            // predicts, and a coverage figure above 100% reads as a bug rather than a rounding.
            var coverage = expected == 0 ? 0d : Math.Min(1d, (double)present / expected);

            return new CoverageReport(stepSeconds, from, to, expected, present, coverage);
        }
    }
}

/// <summary>Resolves API tokens to callers.</summary>
/// <param name="dataSource">The Postgres data source.</param>
public sealed class ApiUserRepository(NpgsqlDataSource dataSource)
{
    private const string FindSql = """
        SELECT id AS "Id", label AS "Label"
        FROM api_users
        WHERE token_hash = @tokenHash AND enabled
        """;

    private readonly NpgsqlDataSource _dataSource = dataSource;

    /// <summary>Finds the enabled caller owning a token hash.</summary>
    /// <param name="tokenHash">SHA-256 of the presented token.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The caller, or null when the token is unknown or disabled.</returns>
    public async Task<ApiUser?> FindByTokenHashAsync(byte[] tokenHash, CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(tokenHash);

        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            return await connection.QuerySingleOrDefaultAsync<ApiUser>(new CommandDefinition(
                FindSql,
                new { tokenHash },
                cancellationToken: cancellationToken)).ConfigureAwait(false);
        }
    }
}

/// <summary>Reads and writes alert rules.</summary>
/// <param name="dataSource">The Postgres data source.</param>
public sealed class AlertRepository(NpgsqlDataSource dataSource)
{
    private const string Columns = """
        id          AS "Id",
        owner_id    AS "OwnerId",
        kind        AS "Kind",
        config::text AS "Config",
        webhook_url AS "WebhookUrl",
        enabled     AS "Enabled",
        last_fired  AS "LastFired",
        created_at  AS "CreatedAt"
        """;

    private readonly NpgsqlDataSource _dataSource = dataSource;

    /// <summary>Lists a caller's rules.</summary>
    /// <param name="ownerId">The owning caller.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The caller's rules, newest first.</returns>
    public async Task<IReadOnlyList<AlertRule>> ListAsync(long ownerId, CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var rows = await connection.QueryAsync<AlertRule>(new CommandDefinition(
                $"SELECT {Columns} FROM alert_rules WHERE owner_id = @ownerId ORDER BY id DESC",
                new { ownerId },
                cancellationToken: cancellationToken)).ConfigureAwait(false);

            return [.. rows];
        }
    }

    /// <summary>Lists every enabled rule, for the dispatcher.</summary>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>All enabled rules.</returns>
    public async Task<IReadOnlyList<AlertRule>> ListEnabledAsync(CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var rows = await connection.QueryAsync<AlertRule>(new CommandDefinition(
                $"SELECT {Columns} FROM alert_rules WHERE enabled ORDER BY id",
                cancellationToken: cancellationToken)).ConfigureAwait(false);

            return [.. rows];
        }
    }

    /// <summary>Creates a rule.</summary>
    /// <param name="ownerId">Owning caller.</param>
    /// <param name="kind">Rule kind.</param>
    /// <param name="configJson">Rule configuration as JSON.</param>
    /// <param name="webhookUrl">Destination, already validated against the host allowlist.</param>
    /// <param name="cancellationToken">Cancels the write.</param>
    /// <returns>The stored rule.</returns>
    public async Task<AlertRule> CreateAsync(
        long ownerId,
        string kind,
        string configJson,
        string webhookUrl,
        CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            return await connection.QuerySingleAsync<AlertRule>(new CommandDefinition(
                $"""
                 INSERT INTO alert_rules (owner_id, kind, config, webhook_url)
                 VALUES (@ownerId, @kind, @configJson::jsonb, @webhookUrl)
                 RETURNING {Columns}
                 """,
                new { ownerId, kind, configJson, webhookUrl },
                cancellationToken: cancellationToken)).ConfigureAwait(false);
        }
    }

    /// <summary>Stamps a rule as fired, so the dispatcher can rate-limit itself.</summary>
    /// <param name="ruleId">The rule.</param>
    /// <param name="cancellationToken">Cancels the write.</param>
    public async Task MarkFiredAsync(long ruleId, CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            await connection.ExecuteAsync(new CommandDefinition(
                "UPDATE alert_rules SET last_fired = now() WHERE id = @ruleId",
                new { ruleId },
                cancellationToken: cancellationToken)).ConfigureAwait(false);
        }
    }
}
