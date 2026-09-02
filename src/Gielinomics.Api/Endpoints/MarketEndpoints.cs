using Gielinomics.Api.Infrastructure;
using Gielinomics.Alerts;
using Gielinomics.Data;

namespace Gielinomics.Api.Endpoints;

/// <summary>
/// A margin candidate with the Grand Exchange tax applied.
/// </summary>
/// <param name="ItemId">Item game ID.</param>
/// <param name="Name">Display name.</param>
/// <param name="BuyPrice">What you would pay — the current instant-sell price.</param>
/// <param name="SellPrice">What you would receive before tax — the current instant-buy price.</param>
/// <param name="Tax">Tax charged on the sale, per unit.</param>
/// <param name="NetMargin">Profit per unit after tax.</param>
/// <param name="BuyLimit">Buy limit per 4 hours.</param>
/// <param name="NetMarginPerLimit">Profit if the whole buy limit is flipped.</param>
/// <param name="HighVolume">Recent instant-buy volume.</param>
/// <param name="LowVolume">Recent instant-sell volume.</param>
/// <param name="ObservedAt">When the prices were observed.</param>
public sealed record MarginCandidate(
    int ItemId,
    string? Name,
    long BuyPrice,
    long SellPrice,
    long Tax,
    long NetMargin,
    int? BuyLimit,
    long NetMarginPerLimit,
    long HighVolume,
    long LowVolume,
    DateTimeOffset ObservedAt);

/// <summary>Cross-item market views.</summary>
public static class MarketEndpoints
{
    /// <summary>
    /// How many raw candidates to pull per requested result.
    /// </summary>
    /// <remarks>
    /// The database ranks by gross spread because it does not know the tax rules. Tax is
    /// capped per item and waived below 50 gp, so gross and net order differently — over-fetch,
    /// re-rank by net, and return the requested number.
    /// </remarks>
    private const int CandidateOverfetch = 5;

    /// <summary>Maps the <c>/api/market</c> routes.</summary>
    /// <param name="app">The route builder.</param>
    /// <returns>The route builder, for chaining.</returns>
    public static IEndpointRouteBuilder MapMarketEndpoints(this IEndpointRouteBuilder app)
    {
        ArgumentNullException.ThrowIfNull(app);

        var group = app.MapGroup("/api/market").WithTags("Market");

        group.MapGet("/movers", GetMoversAsync)
            .WithName("GetMovers")
            .WithSummary("Ranks items by percentage price change over a window.");

        group.MapGet("/spreads", GetSpreadsAsync)
            .WithName("GetSpreads")
            .WithSummary("Scans for flip margins, adjusted for Grand Exchange tax.");

        return app;
    }

    /// <summary>Ranks items by price movement.</summary>
    /// <param name="market">Market reads.</param>
    /// <param name="response">The response, for cache headers.</param>
    /// <param name="window">Lookback window, such as 24h.</param>
    /// <param name="interval">Granularity to measure over.</param>
    /// <param name="minVolume">Minimum traded volume, to exclude illiquid noise.</param>
    /// <param name="limit">Maximum rows.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The movers.</returns>
    private static async Task<IResult> GetMoversAsync(
        MarketQueryRepository market,
        HttpResponse response,
        string? window,
        string? interval,
        long? minVolume,
        int? limit,
        CancellationToken cancellationToken)
    {
        if (!QueryConventions.TryParseWindow(window, TimeSpan.FromHours(24), out var lookback))
        {
            return Results.Problem(
                title: "Invalid window",
                detail: "window must be a positive magnitude followed by m, h, d or w — for example 24h or 7d.",
                statusCode: StatusCodes.Status400BadRequest);
        }

        if (!QueryConventions.TryResolveInterval(interval, out var stepSeconds))
        {
            return Results.Problem(
                title: "Unsupported interval",
                detail: $"interval must be one of: {string.Join(", ", QueryConventions.Intervals.Keys)}.",
                statusCode: StatusCodes.Status400BadRequest);
        }

        var movers = await market
            .GetMoversAsync(
                stepSeconds,
                DateTimeOffset.UtcNow - lookback,
                minVolume ?? 100,
                QueryConventions.ClampPageSize(limit),
                cancellationToken)
            .ConfigureAwait(false);

        QueryConventions.CacheFor(response, TimeSpan.FromMinutes(2));
        return Results.Ok(new { window = lookback, stepSeconds, movers });
    }

    /// <summary>Scans for tax-adjusted flip margins.</summary>
    /// <param name="market">Market reads.</param>
    /// <param name="taxRules">Tax rules in force.</param>
    /// <param name="response">The response, for cache headers.</param>
    /// <param name="minVolume">Minimum volume on both sides of the book.</param>
    /// <param name="limit">Maximum rows.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The candidates, best net margin first.</returns>
    private static async Task<IResult> GetSpreadsAsync(
        MarketQueryRepository market,
        TaxRulesProvider taxRules,
        HttpResponse response,
        long? minVolume,
        int? limit,
        CancellationToken cancellationToken)
    {
        var pageSize = QueryConventions.ClampPageSize(limit, fallback: 25);

        var candidates = await market
            .GetSpreadsAsync(
                minVolume ?? 100,
                TimeSpan.FromHours(1),
                pageSize * CandidateOverfetch,
                cancellationToken)
            .ConfigureAwait(false);

        var rules = taxRules.Current;
        var results = new List<MarginCandidate>(candidates.Count);

        foreach (var candidate in candidates)
        {
            // Buy at the instant-sell price, sell at the instant-buy price. Tax lands on the sale.
            var tax = rules.TaxOn(candidate.ItemId, candidate.High);
            var net = rules.NetMargin(candidate.ItemId, candidate.Low, candidate.High);

            if (net <= 0)
            {
                continue;
            }

            results.Add(new MarginCandidate(
                candidate.ItemId,
                candidate.Name,
                candidate.Low,
                candidate.High,
                tax,
                net,
                candidate.BuyLimit,
                net * (candidate.BuyLimit ?? 1),
                candidate.HighVolume,
                candidate.LowVolume,
                candidate.ObservedAt));
        }

        // Ranked by profit per limit window, not per unit: a 5 gp margin on an item you may
        // buy 13,000 of beats a 5,000 gp margin on one you may buy eight of.
        results.Sort((left, right) => right.NetMarginPerLimit.CompareTo(left.NetMarginPerLimit));

        QueryConventions.CacheFor(response, TimeSpan.FromMinutes(1));

        return Results.Ok(new
        {
            taxRate = rules.Rate,
            taxCapPerItem = rules.CapPerItem,
            // Surfaced so a caller can tell an over-charged estimate from a correct one: until
            // the exempt set has been resolved from the item mapping, exempt items are taxed.
            exemptionsResolved = taxRules.ExemptionsResolved,
            candidates = results.Take(pageSize),
        });
    }
}
