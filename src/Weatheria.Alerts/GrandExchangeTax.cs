namespace Weatheria.Alerts;

/// <summary>
/// The Grand Exchange sales tax, as data rather than a switch statement.
/// </summary>
/// <remarks>
/// The rate has already moved once (1% to 2% on 29 May 2025) and the exempt list moves
/// with game updates, so these are replaceable values. Any margin calculation that ignores
/// tax is wrong, and obviously so to any player.
/// </remarks>
public sealed record GrandExchangeTaxRules
{
    /// <summary>Fraction of the sale price taken as tax.</summary>
    public required decimal Rate { get; init; }

    /// <summary>Sales below this unit price are untaxed entirely.</summary>
    public required long MinimumTaxablePrice { get; init; }

    /// <summary>Maximum tax charged on a single item, regardless of price.</summary>
    public required long CapPerItem { get; init; }

    /// <summary>Item IDs exempt outright.</summary>
    /// <remarks>
    /// TODO: resolve at startup from the item mapping by name — bonds, energy potion,
    /// bronze/iron/steel arrows and darts, mind rune, basic foods, common teleport tablets,
    /// games necklace(8), ring of dueling(8), basic tools. Hand-typed IDs go stale silently.
    /// </remarks>
    public required IReadOnlySet<int> ExemptItemIds { get; init; }

    /// <summary>Rules in force since 29 May 2025.</summary>
    public static GrandExchangeTaxRules Current { get; } = new()
    {
        Rate = 0.02m,
        MinimumTaxablePrice = 50,
        CapPerItem = 5_000_000,
        ExemptItemIds = new HashSet<int>(),
    };

    /// <summary>Tax charged on selling one unit at a given price.</summary>
    /// <param name="itemId">The item being sold.</param>
    /// <param name="unitPrice">Sale price per unit.</param>
    /// <returns>Tax in gp, per unit.</returns>
    public long TaxOn(int itemId, long unitPrice)
        => throw new NotImplementedException("TODO: exempt or sub-threshold -> 0; else min(floor(price * Rate), CapPerItem).");

    /// <summary>Profit from buying at one price and selling at another, after tax.</summary>
    /// <param name="itemId">The item being flipped.</param>
    /// <param name="buyPrice">What you pay per unit.</param>
    /// <param name="sellPrice">What you receive per unit before tax.</param>
    /// <returns>Net profit per unit.</returns>
    public long NetMargin(int itemId, long buyPrice, long sellPrice)
        => throw new NotImplementedException("TODO: sellPrice - TaxOn(itemId, sellPrice) - buyPrice");
}
