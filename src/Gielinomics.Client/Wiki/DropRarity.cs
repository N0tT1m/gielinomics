using System.Globalization;

namespace Gielinomics.Client.Wiki;

/// <summary>
/// Turns the wiki's rarity text into a probability.
/// </summary>
/// <remarks>
/// The values are written for people, not parsers. Across the live table they are, in order of
/// frequency: <c>n/m</c> fractions where the denominator may carry thousands separators and a
/// decimal (<c>1/5,461.33</c>); <c>Always</c>; and several hundred qualitative strings —
/// <c>Varies</c>, <c>Unknown</c>, <c>Rare</c>. The last group has no numeric meaning and is
/// reported as null rather than guessed at, because a made-up probability propagates straight
/// into an expected-value figure somebody would act on.
/// </remarks>
public static class DropRarity
{
    /// <summary>Parses a rarity string into a probability between 0 and 1.</summary>
    /// <param name="rarity">The wiki's rarity text.</param>
    /// <returns>The probability, or null when the text carries no numeric meaning.</returns>
    public static decimal? Parse(string? rarity)
    {
        if (string.IsNullOrWhiteSpace(rarity)) return null;

        var text = rarity.Trim();

        if (string.Equals(text, "Always", StringComparison.OrdinalIgnoreCase)) return 1m;

        var slash = text.IndexOf('/', StringComparison.Ordinal);
        if (slash > 0)
        {
            var numeratorText = text[..slash].Trim();
            var denominatorText = text[(slash + 1)..].Trim();

            if (TryParseNumber(numeratorText, out var numerator)
                && TryParseNumber(denominatorText, out var denominator)
                && denominator > 0)
            {
                var probability = numerator / denominator;
                return probability is >= 0m and <= 1m ? probability : null;
            }

            return null;
        }

        // A bare number is already a probability in the sources that use it.
        return TryParseNumber(text, out var value) && value is >= 0m and <= 1m ? value : null;
    }

    /// <summary>Parses a number that may carry thousands separators and a decimal point.</summary>
    /// <param name="text">The number.</param>
    /// <param name="value">The parsed value.</param>
    /// <returns>True when it parsed.</returns>
    private static bool TryParseNumber(string text, out decimal value)
        => decimal.TryParse(
            text,
            NumberStyles.AllowDecimalPoint | NumberStyles.AllowThousands,
            CultureInfo.InvariantCulture,
            out value);
}
