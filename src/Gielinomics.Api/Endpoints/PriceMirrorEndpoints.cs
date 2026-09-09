using Gielinomics.Api.Infrastructure;
using Gielinomics.Data;

namespace Gielinomics.Api.Endpoints;

/// <summary>One item's last observed trade, in upstream field names.</summary>
/// <param name="High">Last price a buy offer completed at.</param>
/// <param name="HighTime">Unix seconds of that trade.</param>
/// <param name="Low">Last price a sell offer completed at.</param>
/// <param name="LowTime">Unix seconds of that trade.</param>
public sealed record LatestEntry(long? High, long? HighTime, long? Low, long? LowTime);

/// <summary>One item's window averages, in upstream field names.</summary>
/// <param name="AvgHighPrice">Volume-weighted instant-buy price over the window.</param>
/// <param name="HighPriceVolume">Units bought instantly over the window.</param>
/// <param name="AvgLowPrice">Volume-weighted instant-sell price over the window.</param>
/// <param name="LowPriceVolume">Units sold instantly over the window.</param>
public sealed record WindowEntry(long? AvgHighPrice, long HighPriceVolume, long? AvgLowPrice, long LowPriceVolume);

/// <summary>An item's catalogue row, in upstream field names.</summary>
/// <param name="Id">Item game ID.</param>
/// <param name="Name">Display name.</param>
/// <param name="Examine">Examine text.</param>
/// <param name="Members">Members-only flag.</param>
/// <param name="Limit">Buy limit per 4 hours.</param>
/// <param name="HighAlch">High alchemy value.</param>
/// <param name="LowAlch">Low alchemy value.</param>
/// <param name="Value">Store value.</param>
/// <param name="Icon">Icon filename.</param>
public sealed record MappingRow(
    int Id,
    string Name,
    string? Examine,
    bool Members,
    int? Limit,
    long? HighAlch,
    long? LowAlch,
    long? Value,
    string? Icon);

/// <summary>A whole-market response, keyed by item ID as a string.</summary>
/// <typeparam name="T">The per-item payload.</typeparam>
/// <param name="Data">One entry per item.</param>
/// <param name="Timestamp">Start of the window, as Unix seconds. Null for point-in-time reads.</param>
public sealed record MarketSnapshot<T>(IReadOnlyDictionary<string, T> Data, long? Timestamp);

/// <summary>
/// Whole-market price routes shaped like the upstream real-time prices API.
/// </summary>
/// <remarks>
/// <para>
/// Deliberately a separate group from <c>/api/items</c> rather than more routes on it. These
/// return every item in one body and use upstream's field names (<c>avgHighPrice</c>, not
/// <c>AvgHigh</c>), which is a different contract with a different audience: a client already
/// written against the wiki's API, which repoints here by changing a base URL. Mixing that
/// vocabulary into the platform's own routes would leave neither legible.
/// </para>
/// <para>
/// The names are camelCase on the wire because the API serialises that way throughout, which
/// is what upstream uses too — so the shapes coincide without a custom naming policy.
/// </para>
/// </remarks>
public static class PriceMirrorEndpoints
{
    /// <summary>Windows this mirror will aggregate, keyed by their upstream route name.</summary>
    /// <remarks>
    /// <para>
    /// Upstream serves 5m, 1h, 6h and 24h. The bars retained here are 5-minute and hourly, so
    /// each window aggregates from the finest granularity that covers it without reading more
    /// rows than the answer needs.
    /// </para>
    /// <para>
    /// Public because it is the contract, not an implementation detail: it is the set of window
    /// names a client can ask for, the same way <see cref="QueryConventions.Intervals"/> is the
    /// set of interval names. Every step here must be a value the ingest workers actually write
    /// to <c>price_series</c>, or the query returns nothing and looks like a quiet market.
    /// </para>
    /// </remarks>
    public static IReadOnlyDictionary<string, (TimeSpan Window, int StepSeconds)> Windows { get; } =
        new Dictionary<string, (TimeSpan, int)>(StringComparer.OrdinalIgnoreCase)
        {
            ["5m"] = (TimeSpan.FromMinutes(5), 300),
            ["1h"] = (TimeSpan.FromHours(1), 300),
            ["6h"] = (TimeSpan.FromHours(6), 3600),
            ["24h"] = (TimeSpan.FromHours(24), 3600),
        };

    /// <summary>Maps the <c>/api/prices</c> routes.</summary>
    /// <param name="app">The route builder.</param>
    /// <returns>The route builder, for chaining.</returns>
    public static IEndpointRouteBuilder MapPriceMirrorEndpoints(this IEndpointRouteBuilder app)
    {
        ArgumentNullException.ThrowIfNull(app);

        var group = app.MapGroup("/api/prices").WithTags("Prices");

        group.MapGet("/mapping", GetMappingAsync)
            .WithName("GetPriceMapping")
            .WithSummary("Every catalogued tradeable item, in upstream mapping shape.")
            .Produces<IReadOnlyList<MappingRow>>();

        group.MapGet("/latest", GetLatestAsync)
            .WithName("GetLatestPrices")
            .WithSummary("The most recent observed trade for every item.")
            .Produces<MarketSnapshot<LatestEntry>>();

        group.MapGet("/{window}", GetWindowAsync)
            .WithName("GetWindowPrices")
            .WithSummary("Volume-weighted averages for every item over a trailing window.")
            .Produces<MarketSnapshot<WindowEntry>>()
            .ProducesProblem(StatusCodes.Status400BadRequest);

        return app;
    }

    /// <summary>Serves the item catalogue.</summary>
    /// <param name="prices">Mirror reads.</param>
    /// <param name="response">The response, for cache headers.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The catalogue.</returns>
    private static async Task<IResult> GetMappingAsync(
        PriceMirrorRepository prices,
        HttpResponse response,
        CancellationToken cancellationToken)
    {
        var rows = await prices.GetMappingAsync(cancellationToken).ConfigureAwait(false);

        // The mapping sync runs daily; a client caching it for an hour is still an order of
        // magnitude fresher than the data behind it.
        QueryConventions.CacheFor(response, TimeSpan.FromHours(1));

        return Results.Ok(rows.Select(row => new MappingRow(
            row.Id,
            row.Name,
            row.Examine,
            row.Members,
            row.Limit,
            row.HighAlch,
            row.LowAlch,
            row.Value,
            row.Icon)).ToList());
    }

    /// <summary>Serves the last observed trade per item.</summary>
    /// <param name="prices">Mirror reads.</param>
    /// <param name="response">The response, for cache headers.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The snapshot.</returns>
    private static async Task<IResult> GetLatestAsync(
        PriceMirrorRepository prices,
        HttpResponse response,
        CancellationToken cancellationToken)
    {
        var rows = await prices.GetLatestAsync(cancellationToken: cancellationToken).ConfigureAwait(false);

        var data = rows.ToDictionary(
            row => row.ItemId.ToString(System.Globalization.CultureInfo.InvariantCulture),
            row => new LatestEntry(
                row.High,
                Unix(row.HighTime),
                row.Low,
                Unix(row.LowTime)));

        QueryConventions.CacheFor(response, TimeSpan.FromSeconds(30));
        return Results.Ok(new MarketSnapshot<LatestEntry>(data, null));
    }

    /// <summary>Serves window averages for every item.</summary>
    /// <param name="prices">Mirror reads.</param>
    /// <param name="response">The response, for cache headers.</param>
    /// <param name="window">Window name: 5m, 1h, 6h or 24h.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The snapshot.</returns>
    private static async Task<IResult> GetWindowAsync(
        PriceMirrorRepository prices,
        HttpResponse response,
        string window,
        CancellationToken cancellationToken)
    {
        if (!Windows.TryGetValue(window, out var resolved))
        {
            return Results.Problem(
                title: "Unsupported window",
                detail: $"window must be one of: {string.Join(", ", Windows.Keys)}.",
                statusCode: StatusCodes.Status400BadRequest);
        }

        var since = DateTimeOffset.UtcNow - resolved.Window;
        var rows = await prices
            .GetWindowAsync(resolved.StepSeconds, since, cancellationToken)
            .ConfigureAwait(false);

        var data = rows.ToDictionary(
            row => row.ItemId.ToString(System.Globalization.CultureInfo.InvariantCulture),
            row => new WindowEntry(
                row.AvgHighPrice,
                row.HighPriceVolume,
                row.AvgLowPrice,
                row.LowPriceVolume));

        QueryConventions.CacheFor(response, TimeSpan.FromMinutes(1));
        return Results.Ok(new MarketSnapshot<WindowEntry>(data, since.ToUnixTimeSeconds()));
    }

    /// <summary>Converts an instant to Unix seconds, preserving null.</summary>
    /// <param name="value">The instant.</param>
    /// <returns>Unix seconds, or null.</returns>
    private static long? Unix(DateTimeOffset? value) => value?.ToUnixTimeSeconds();
}
