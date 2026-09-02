using Gielinomics.Api.Infrastructure;
using Gielinomics.Data;

namespace Gielinomics.Api.Endpoints;

/// <summary>A monster and its drop table, priced against retained history.</summary>
/// <param name="Monster">Reference data, or null when the monster is unknown to the wiki sync.</param>
/// <param name="TotalExpectedValue">
/// Sum of the priced drops' expected values — roughly the gp a kill is worth. Drops whose
/// rarity the wiki records qualitatively contribute nothing, so this is a floor, not a total.
/// </param>
/// <param name="UnpricedDrops">How many rows had no usable rarity or no retained price.</param>
/// <param name="Drops">The drop table, most valuable per kill first.</param>
public sealed record MonsterDropsResponse(
    Monster? Monster,
    decimal TotalExpectedValue,
    int UnpricedDrops,
    IReadOnlyList<DropTableEntry> Drops);

/// <summary>Everything known to drop an item.</summary>
/// <param name="ItemId">The item.</param>
/// <param name="Sources">The sources, most common first.</param>
public sealed record ItemDropSourcesResponse(int ItemId, IReadOnlyList<DropTableEntry> Sources);

/// <summary>Equipment ranked by one stat against price.</summary>
/// <param name="Stat">The stat ranked on.</param>
/// <param name="Slot">The slot filtered to, if any.</param>
/// <param name="CheapestFirst">Whether the ranking is by gp per point rather than by raw stat.</param>
/// <param name="TradeableOnly">Whether untradeable variants were excluded.</param>
/// <param name="Options">The ranking.</param>
public sealed record GearResponse(
    string Stat,
    string? Slot,
    bool CheapestFirst,
    bool TradeableOnly,
    IReadOnlyList<GearOption> Options);

/// <summary>
/// Wiki structured data: equipment bonuses, drop tables, monsters.
/// </summary>
/// <remarks>
/// These are the routes the upstream APIs cannot answer at all. The prices API knows what a
/// whip costs; only the join to the wiki's drop table knows what an abyssal demon kill is
/// worth, and only the join to its bonuses knows what a strength point costs.
/// </remarks>
public static class WikiEndpoints
{
    /// <summary>Maps the drop table, gear and monster routes.</summary>
    /// <param name="app">The route builder.</param>
    /// <returns>The route builder, for chaining.</returns>
    public static IEndpointRouteBuilder MapWikiEndpoints(this IEndpointRouteBuilder app)
    {
        ArgumentNullException.ThrowIfNull(app);

        app.MapGet("/api/monsters/{name}/drops", GetMonsterDropsAsync)
            .WithTags("Monsters")
            .WithName("GetMonsterDrops")
            .WithSummary("A monster's drop table, priced against retained market history.")
            .Produces<MonsterDropsResponse>()
            .Produces(StatusCodes.Status404NotFound);

        app.MapGet("/api/items/{id:int}/bonuses", GetBonusesAsync)
            .WithTags("Items")
            .WithName("GetItemBonuses")
            .WithSummary("Equipment bonuses for an item.")
            .Produces<ItemBonuses>()
            .Produces(StatusCodes.Status404NotFound);

        app.MapGet("/api/items/{id:int}/drops", GetItemDropsAsync)
            .WithTags("Items")
            .WithName("GetItemDropSources")
            .WithSummary("Everything known to drop this item.")
            .Produces<ItemDropSourcesResponse>();

        app.MapGet("/api/gear", GetGearAsync)
            .WithTags("Market")
            .WithName("GetGear")
            .WithSummary("Ranks equipment by a stat against its current price.")
            .Produces<GearResponse>()
            .ProducesProblem(StatusCodes.Status400BadRequest);

        return app;
    }

    /// <summary>A monster's priced drop table.</summary>
    /// <param name="wiki">Wiki data reads.</param>
    /// <param name="response">The response, for cache headers.</param>
    /// <param name="name">Monster name.</param>
    /// <param name="limit">Maximum rows.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The drop table, or 404.</returns>
    private static async Task<IResult> GetMonsterDropsAsync(
        WikiRepository wiki,
        HttpResponse response,
        string name,
        int? limit,
        CancellationToken cancellationToken)
    {
        var drops = await wiki
            .GetDropsBySourceAsync(name, Math.Clamp(limit ?? 100, 1, QueryConventions.MaxPageSize), cancellationToken)
            .ConfigureAwait(false);

        var monster = await wiki.GetMonsterAsync(name, cancellationToken).ConfigureAwait(false);

        if (drops.Count == 0 && monster is null)
        {
            return Results.Problem(
                title: "Monster not found",
                detail: $"'{name}' has no drop table in the wiki sync. It may not have run yet.",
                statusCode: StatusCodes.Status404NotFound);
        }

        // A drop the wiki describes as 'Varies' contributes nothing rather than a guess, which
        // makes this a floor on the kill's value rather than an estimate of it.
        var total = drops.Sum(drop => drop.ExpectedValue ?? 0m);
        var unpriced = drops.Count(drop => drop.ExpectedValue is null);

        QueryConventions.CacheFor(response, TimeSpan.FromMinutes(5));
        return Results.Ok(new MonsterDropsResponse(monster, total, unpriced, drops));
    }

    /// <summary>Equipment bonuses for one item.</summary>
    /// <param name="wiki">Wiki data reads.</param>
    /// <param name="response">The response, for cache headers.</param>
    /// <param name="id">Item game ID.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The bonuses, or 404 when the item is not equipment.</returns>
    private static async Task<IResult> GetBonusesAsync(
        WikiRepository wiki,
        HttpResponse response,
        int id,
        CancellationToken cancellationToken)
    {
        var bonuses = await wiki.GetBonusesAsync(id, cancellationToken).ConfigureAwait(false);
        if (bonuses is null)
        {
            return Results.NotFound();
        }

        // Reference data that moves weekly at most.
        QueryConventions.CacheFor(response, TimeSpan.FromHours(6));
        return Results.Ok(bonuses);
    }

    /// <summary>Everything that drops an item.</summary>
    /// <param name="wiki">Wiki data reads.</param>
    /// <param name="response">The response, for cache headers.</param>
    /// <param name="id">Item game ID.</param>
    /// <param name="limit">Maximum rows.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The sources.</returns>
    private static async Task<IResult> GetItemDropsAsync(
        WikiRepository wiki,
        HttpResponse response,
        int id,
        int? limit,
        CancellationToken cancellationToken)
    {
        var sources = await wiki
            .GetDropSourcesAsync(id, Math.Clamp(limit ?? 50, 1, QueryConventions.MaxPageSize), cancellationToken)
            .ConfigureAwait(false);

        QueryConventions.CacheFor(response, TimeSpan.FromHours(6));
        return Results.Ok(new ItemDropSourcesResponse(id, sources));
    }

    /// <summary>Ranks equipment by a stat.</summary>
    /// <param name="wiki">Wiki data reads.</param>
    /// <param name="response">The response, for cache headers.</param>
    /// <param name="stat">Which stat to rank on.</param>
    /// <param name="slot">Equipment slot to restrict to.</param>
    /// <param name="maxPrice">Budget ceiling.</param>
    /// <param name="cheapestFirst">Rank by gp per point instead of by raw stat.</param>
    /// <param name="includeUntradeable">Include variants with no market price. Off by default.</param>
    /// <param name="limit">Maximum rows.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The ranking.</returns>
    private static async Task<IResult> GetGearAsync(
        WikiRepository wiki,
        HttpResponse response,
        string? stat,
        string? slot,
        long? maxPrice,
        bool? cheapestFirst,
        bool? includeUntradeable,
        int? limit,
        CancellationToken cancellationToken)
    {
        var chosen = stat ?? "strength";

        if (!GearStats.TryResolve(chosen, out _))
        {
            return Results.Problem(
                title: "Unknown stat",
                detail: $"stat must be one of: {string.Join(", ", GearStats.Names.Order(StringComparer.Ordinal))}.",
                statusCode: StatusCodes.Status400BadRequest);
        }

        var byValue = cheapestFirst ?? false;
        var tradeableOnly = !(includeUntradeable ?? false);

        var options = await wiki
            .GetGearAsync(chosen, slot, maxPrice, byValue, tradeableOnly, QueryConventions.ClampPageSize(limit), cancellationToken)
            .ConfigureAwait(false);

        QueryConventions.CacheFor(response, TimeSpan.FromMinutes(10));
        return Results.Ok(new GearResponse(chosen, slot, byValue, tradeableOnly, options));
    }
}
