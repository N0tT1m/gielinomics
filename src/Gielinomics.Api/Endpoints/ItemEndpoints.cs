using System.Globalization;
using Gielinomics.Api.Infrastructure;
using Gielinomics.Data;

namespace Gielinomics.Api.Endpoints;

/// <summary>Item reference data and retained price history.</summary>
public static class ItemEndpoints
{
    /// <summary>Maps the <c>/api/items</c> routes.</summary>
    /// <param name="app">The route builder.</param>
    /// <returns>The route builder, for chaining.</returns>
    public static IEndpointRouteBuilder MapItemEndpoints(this IEndpointRouteBuilder app)
    {
        ArgumentNullException.ThrowIfNull(app);

        var group = app.MapGroup("/api/items").WithTags("Items");

        group.MapGet("/", SearchAsync)
            .WithName("SearchItems")
            .WithSummary("Searches items by name, membership and buy limit.");

        group.MapGet("/{id:int}", GetAsync)
            .WithName("GetItem")
            .WithSummary("Fetches one item's reference metadata.");

        group.MapGet("/{id:int}/prices", GetPricesAsync)
            .WithName("GetItemPrices")
            .WithSummary("Fetches this platform's retained price history for an item.");

        group.MapGet("/{id:int}/stats", GetStatsAsync)
            .WithName("GetItemStats")
            .WithSummary("Computes volatility, mean spread and liquidity over retained history.");

        return app;
    }

    /// <summary>Searches items.</summary>
    /// <param name="market">Market reads.</param>
    /// <param name="response">The response, for cache headers.</param>
    /// <param name="search">Case-insensitive name substring.</param>
    /// <param name="members">Members-only filter.</param>
    /// <param name="minBuyLimit">Minimum buy limit.</param>
    /// <param name="cursor">Cursor from a previous page.</param>
    /// <param name="limit">Page size.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>A page of items.</returns>
    private static async Task<IResult> SearchAsync(
        MarketQueryRepository market,
        HttpResponse response,
        string? search,
        bool? members,
        int? minBuyLimit,
        string? cursor,
        int? limit,
        CancellationToken cancellationToken)
    {
        var after = 0;
        if (!string.IsNullOrWhiteSpace(cursor) && !int.TryParse(cursor, NumberStyles.None, CultureInfo.InvariantCulture, out after))
        {
            return Results.Problem(
                title: "Invalid cursor",
                detail: "cursor must be a value returned as nextCursor by a previous request.",
                statusCode: StatusCodes.Status400BadRequest);
        }

        var pageSize = QueryConventions.ClampPageSize(limit);
        var items = await market
            .SearchItemsAsync(search, members, minBuyLimit, after, pageSize, cancellationToken)
            .ConfigureAwait(false);

        // A full page means there may be more; a short page is definitively the last one.
        var nextCursor = items.Count == pageSize
            ? items[^1].Id.ToString(CultureInfo.InvariantCulture)
            : null;

        QueryConventions.CacheFor(response, TimeSpan.FromMinutes(5));
        return Results.Ok(new Page<ItemSummary>(items, nextCursor));
    }

    /// <summary>
    /// Fetches one item, with an ETag.
    /// </summary>
    /// <remarks>
    /// Item metadata changes at most daily, when the mapping sync runs. <c>last_seen</c> moves
    /// on every price poll, so the tag is built from the fields a client would actually notice
    /// changing rather than from the whole row.
    /// </remarks>
    /// <param name="market">Market reads.</param>
    /// <param name="http">The request, for conditional handling.</param>
    /// <param name="id">Item game ID.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The item, 304, or 404.</returns>
    private static async Task<IResult> GetAsync(
        MarketQueryRepository market,
        HttpContext http,
        int id,
        CancellationToken cancellationToken)
    {
        var item = await market.GetItemAsync(id, cancellationToken).ConfigureAwait(false);
        if (item is null)
        {
            return Results.NotFound();
        }

        var etag = $"\"{item.Id}-{(item.IsStub ? 0 : 1)}-{item.Name?.Length ?? 0}-{item.BuyLimit ?? -1}-{item.Value ?? -1}\"";

        if (http.Request.Headers.IfNoneMatch.Contains(etag))
        {
            return Results.StatusCode(StatusCodes.Status304NotModified);
        }

        http.Response.Headers.ETag = etag;
        QueryConventions.CacheFor(http.Response, TimeSpan.FromHours(1));

        return Results.Ok(item);
    }

    /// <summary>Fetches retained price history.</summary>
    /// <param name="market">Market reads.</param>
    /// <param name="response">The response, for cache headers.</param>
    /// <param name="id">Item game ID.</param>
    /// <param name="from">Inclusive lower bound. Defaults to 24 hours ago.</param>
    /// <param name="to">Inclusive upper bound. Defaults to now.</param>
    /// <param name="interval">Granularity: 5m, 1h or 1d.</param>
    /// <param name="limit">Maximum bars.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The series.</returns>
    private static async Task<IResult> GetPricesAsync(
        MarketQueryRepository market,
        HttpResponse response,
        int id,
        DateTimeOffset? from,
        DateTimeOffset? to,
        string? interval,
        int? limit,
        CancellationToken cancellationToken)
    {
        if (!QueryConventions.TryResolveInterval(interval, out var stepSeconds))
        {
            return Results.Problem(
                title: "Unsupported interval",
                detail: $"interval must be one of: {string.Join(", ", QueryConventions.Intervals.Keys)}.",
                statusCode: StatusCodes.Status400BadRequest);
        }

        var upper = to ?? DateTimeOffset.UtcNow;
        var lower = from ?? upper.AddDays(-1);

        if (lower >= upper)
        {
            return Results.Problem(
                title: "Invalid range",
                detail: "from must be earlier than to.",
                statusCode: StatusCodes.Status400BadRequest);
        }

        var points = await market
            .GetPricesAsync(id, stepSeconds, lower, upper, Math.Clamp(limit ?? QueryConventions.MaxPricePoints, 1, QueryConventions.MaxPricePoints), cancellationToken)
            .ConfigureAwait(false);

        QueryConventions.CacheFor(response, TimeSpan.FromMinutes(1));
        return Results.Ok(new { itemId = id, stepSeconds, from = lower, to = upper, points });
    }

    /// <summary>Computes derived statistics.</summary>
    /// <param name="market">Market reads.</param>
    /// <param name="response">The response, for cache headers.</param>
    /// <param name="id">Item game ID.</param>
    /// <param name="window">Lookback window, such as 7d.</param>
    /// <param name="interval">Granularity to compute over.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The statistics.</returns>
    private static async Task<IResult> GetStatsAsync(
        MarketQueryRepository market,
        HttpResponse response,
        int id,
        string? window,
        string? interval,
        CancellationToken cancellationToken)
    {
        if (!QueryConventions.TryResolveInterval(interval, out var stepSeconds))
        {
            return Results.Problem(
                title: "Unsupported interval",
                detail: $"interval must be one of: {string.Join(", ", QueryConventions.Intervals.Keys)}.",
                statusCode: StatusCodes.Status400BadRequest);
        }

        if (!QueryConventions.TryParseWindow(window, TimeSpan.FromDays(7), out var lookback))
        {
            return Results.Problem(
                title: "Invalid window",
                detail: "window must be a positive magnitude followed by m, h, d or w — for example 24h or 7d.",
                statusCode: StatusCodes.Status400BadRequest);
        }

        var stats = await market
            .GetStatsAsync(id, stepSeconds, DateTimeOffset.UtcNow - lookback, cancellationToken)
            .ConfigureAwait(false);

        QueryConventions.CacheFor(response, TimeSpan.FromMinutes(5));
        return Results.Ok(stats);
    }
}
