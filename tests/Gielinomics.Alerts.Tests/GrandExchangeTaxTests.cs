using Gielinomics.Alerts;
using Xunit;

namespace Gielinomics.Alerts.Tests;

/// <summary>
/// Tax rules verified against the 29 May 2025 change: 2%, capped at 5M per item,
/// waived under 50 gp. Any margin calculation that ignores these is wrong, and
/// obviously so to any player.
/// </summary>
public class GrandExchangeTaxTests
{
    private static readonly GrandExchangeTaxRules Rules = GrandExchangeTaxRules.Current;

    [Theory]
    [InlineData(0, 0)]
    [InlineData(1, 0)]
    [InlineData(49, 0)]      // Under the threshold entirely.
    [InlineData(50, 1)]      // First taxable price: 2% of 50 is exactly 1.
    [InlineData(99, 1)]      // 1.98 floors to 1, in the player's favour.
    [InlineData(100, 2)]
    [InlineData(823_005, 16_460)]
    public void TaxOn_applies_the_rate_above_the_threshold(long price, long expected)
        => Assert.Equal(expected, Rules.TaxOn(itemId: 4151, price));

    [Fact]
    public void TaxOn_caps_at_five_million_per_item()
    {
        // 2% of 500M would be 10M. The cap is what makes high-value flips viable at all,
        // and ignoring it understates profit on exactly the items people care about.
        Assert.Equal(5_000_000, Rules.TaxOn(itemId: 20_997, unitPrice: 500_000_000));
        Assert.Equal(5_000_000, Rules.TaxOn(itemId: 20_997, unitPrice: 250_000_000));

        // Just below the cap boundary: 2% of 249,999,999 is 4,999,999.98 -> 4,999,999.
        Assert.Equal(4_999_999, Rules.TaxOn(itemId: 20_997, unitPrice: 249_999_999));
    }

    [Fact]
    public void TaxOn_is_waived_for_exempt_items()
    {
        var resolved = Rules.ResolveExemptions([(13_190, "Old school bond"), (4151, "Abyssal whip")]);

        Assert.Equal(0, resolved.TaxOn(13_190, 8_000_000));
        Assert.Equal(160_000, resolved.TaxOn(4151, 8_000_000));
    }

    [Fact]
    public void NetMargin_subtracts_the_tax_from_the_sale_side_only()
    {
        // Buy at 801,051, sell at 823,005. Tax lands on the sale: 16,460.
        var margin = Rules.NetMargin(itemId: 4151, buyPrice: 801_051, sellPrice: 823_005);

        Assert.Equal(823_005 - 16_460 - 801_051, margin);
        Assert.Equal(5_494, margin);
    }

    [Fact]
    public void NetMargin_goes_negative_when_the_tax_eats_the_spread()
    {
        // A 1% gross spread does not survive a 2% tax. Reporting this as a flip is the
        // single most expensive thing a margin scanner can get wrong.
        var margin = Rules.NetMargin(itemId: 4151, buyPrice: 1_000_000, sellPrice: 1_010_000);

        Assert.True(margin < 0);
        Assert.Equal(1_010_000 - 20_200 - 1_000_000, margin);
    }

    [Theory]
    [InlineData("Old school bond", true)]
    [InlineData("Bronze arrow", true)]
    [InlineData("Steel dart", true)]
    [InlineData("Mind rune", true)]
    [InlineData("Lobster", true)]
    [InlineData("Meat pie", true)]
    [InlineData("Spade", true)]
    [InlineData("Varrock teleport", true)]   // No dose suffix; must match exactly.
    [InlineData("Energy potion(4)", true)]   // Dosed; must match by stem.
    [InlineData("Energy potion(1)", true)]
    [InlineData("Games necklace(8)", true)]
    [InlineData("Ring of dueling(8)", true)]
    [InlineData("Games necklace(1)", false)] // Only the full charge is exempt.
    [InlineData("Ring of dueling(4)", false)]
    [InlineData("Abyssal whip", false)]
    [InlineData("Super energy potion(4)", false)]
    [InlineData("Twisted bow", false)]
    public void ResolveExemptions_matches_the_published_list(string name, bool exempt)
    {
        var resolved = Rules.ResolveExemptions([(1, name)]);

        Assert.Equal(exempt, resolved.ExemptItemIds.Contains(1));
    }

    [Fact]
    public void ResolveExemptions_is_case_insensitive_and_skips_unnamed_stubs()
    {
        var resolved = Rules.ResolveExemptions([(1, "LOBSTER"), (2, null), (3, "lobster")]);

        Assert.Contains(1, resolved.ExemptItemIds);
        Assert.Contains(3, resolved.ExemptItemIds);
        Assert.DoesNotContain(2, resolved.ExemptItemIds);
    }

    [Fact]
    public void Current_ships_with_no_exemptions_so_tax_is_over_charged_not_under_charged()
    {
        // Before the item mapping is available there is nothing to resolve names against.
        // Erring towards charging tax costs a missed flip; erring the other way costs gp.
        Assert.Empty(Rules.ExemptItemIds);
        Assert.Equal(0.02m, Rules.Rate);
        Assert.Equal(50, Rules.MinimumTaxablePrice);
        Assert.Equal(5_000_000, Rules.CapPerItem);
    }
}
