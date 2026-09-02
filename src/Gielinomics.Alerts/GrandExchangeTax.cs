namespace Gielinomics.Alerts;

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
    /// Populated by <see cref="ResolveExemptions"/> from the live item mapping rather than
    /// hand-typed. Hand-typed IDs go stale silently: an item gets a new variant, the ID it
    /// used to have gets reused, and the margin scan quietly starts lying.
    /// </remarks>
    public required IReadOnlySet<int> ExemptItemIds { get; init; }

    /// <summary>
    /// Names exempt from the tax, matched exactly and case-insensitively.
    /// </summary>
    /// <remarks>
    /// The list the wiki publishes, transcribed as names because names are what the mapping
    /// carries and what a game update changes visibly. Verified against the 29 May 2025 rules.
    /// </remarks>
    public static IReadOnlySet<string> ExemptItemNames { get; } = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
    {
        "Old school bond",
        "Bronze arrow", "Iron arrow", "Steel arrow",
        "Bronze dart", "Iron dart", "Steel dart",
        "Mind rune",
        "Bass", "Bread", "Cake", "Chicken", "Herring", "Lobster",
        "Mackerel", "Pike", "Salmon", "Shrimps", "Tuna", "Meat pie",
        "Chisel", "Hammer", "Needle", "Rake", "Saw", "Secateurs", "Spade",

        // Teleport tablets carry no dose suffix, so they belong here rather than in the
        // stem set below, which only ever matches a parenthesised name.
        "Varrock teleport", "Lumbridge teleport", "Falador teleport", "Camelot teleport",
        "Ardougne teleport", "Watchtower teleport", "Teleport to house",

        // Only the fully charged variant is exempt, which is why these are exact names and
        // not stems. Exempting every charge would under-charge the tax and overstate margins.
        "Games necklace(8)", "Ring of dueling(8)",
    };

    /// <summary>
    /// Name stems whose dosed or charged variants are all exempt.
    /// </summary>
    /// <remarks>
    /// A stem matches the bare name and any parenthesised suffix, so "Energy potion" covers
    /// "Energy potion(1)" through "(4)" without enumerating doses that a game update can add to.
    /// </remarks>
    public static IReadOnlySet<string> ExemptItemNameStems { get; } = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
    {
        "Energy potion",
    };

    /// <summary>Rules in force since 29 May 2025, with exemptions not yet resolved.</summary>
    /// <remarks>
    /// <see cref="ExemptItemIds"/> is empty here by design. Resolving it needs the item
    /// mapping, which is a runtime input — call <see cref="ResolveExemptions"/> once the
    /// mapping is loaded. Until then the tax is over-charged, never under-charged, which is
    /// the safe direction for a margin estimate.
    /// </remarks>
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
    {
        if (unitPrice < MinimumTaxablePrice || ExemptItemIds.Contains(itemId))
        {
            return 0;
        }

        // Floor, not round: the game rounds the player's way on a fractional gp.
        var tax = (long)decimal.Floor(unitPrice * Rate);
        return Math.Min(tax, CapPerItem);
    }

    /// <summary>Profit from buying at one price and selling at another, after tax.</summary>
    /// <param name="itemId">The item being flipped.</param>
    /// <param name="buyPrice">What you pay per unit.</param>
    /// <param name="sellPrice">What you receive per unit before tax.</param>
    /// <returns>Net profit per unit. Negative when the tax eats the spread.</returns>
    public long NetMargin(int itemId, long buyPrice, long sellPrice)
        => sellPrice - TaxOn(itemId, sellPrice) - buyPrice;

    /// <summary>
    /// Resolves the exempt name list against a live item mapping.
    /// </summary>
    /// <param name="items">Item IDs paired with their display names.</param>
    /// <returns>A copy of these rules with <see cref="ExemptItemIds"/> populated.</returns>
    /// <exception cref="ArgumentNullException"><paramref name="items"/> is null.</exception>
    public GrandExchangeTaxRules ResolveExemptions(IEnumerable<(int Id, string? Name)> items)
    {
        ArgumentNullException.ThrowIfNull(items);

        var ids = new HashSet<int>();
        foreach (var (id, name) in items)
        {
            if (name is not null && IsExemptName(name))
            {
                ids.Add(id);
            }
        }

        return this with { ExemptItemIds = ids };
    }

    /// <summary>Whether an item name falls under the published exemptions.</summary>
    /// <param name="name">The item's display name.</param>
    /// <returns>True when exempt.</returns>
    internal static bool IsExemptName(string name)
    {
        if (ExemptItemNames.Contains(name))
        {
            return true;
        }

        // "Energy potion(4)" -> stem "Energy potion". Anything before the first '(' only;
        // a name with no parenthesis was already covered by the exact set above.
        var parenthesis = name.IndexOf('(', StringComparison.Ordinal);
        if (parenthesis <= 0)
        {
            return false;
        }

        return ExemptItemNameStems.Contains(name[..parenthesis].TrimEnd());
    }
}
