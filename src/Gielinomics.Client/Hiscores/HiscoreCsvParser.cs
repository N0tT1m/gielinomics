using System.Globalization;

namespace Gielinomics.Client.Hiscores;

/// <summary>
/// Decodes the positional <c>index_lite.ws</c> CSV.
/// </summary>
/// <remarks>
/// The fallback path. <c>index_lite.json</c> names every field and is used first; this exists
/// for the day that endpoint changes or disappears, which is exactly why the plan called for
/// keeping it. Here, and only here, the line ordering really is the entire contract.
/// </remarks>
public static class HiscoreCsvParser
{
    /// <summary>
    /// Parses a CSV hiscore response.
    /// </summary>
    /// <param name="csv">The response body.</param>
    /// <param name="mapping">Mapping used to name each positional line.</param>
    /// <param name="unknownLineCount">
    /// Lines beyond what <paramref name="mapping"/> accounts for. Non-zero means Jagex has
    /// appended something and the mapping needs updating — the caller is expected to alarm on
    /// it rather than let the extra fields vanish.
    /// </param>
    /// <returns>The decoded profile.</returns>
    /// <exception cref="ArgumentNullException">Any argument is null.</exception>
    /// <exception cref="FormatException">A line is not in the documented shape.</exception>
    public static HiscoreProfile Parse(string csv, HiscoreMapping mapping, out int unknownLineCount)
    {
        ArgumentNullException.ThrowIfNull(csv);
        ArgumentNullException.ThrowIfNull(mapping);

        var lines = csv.Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);

        // Counted, not truncated silently. Trailing lines the mapping cannot name are new
        // content, and dropping them without a word is how a dataset quietly stops being complete.
        unknownLineCount = Math.Max(0, lines.Length - mapping.ExpectedLineCount);

        var skills = new List<HiscoreSkill>(mapping.SkillNames.Count);
        for (var i = 0; i < mapping.SkillNames.Count && i < lines.Length; i++)
        {
            var parts = Split(lines[i], expected: 3, i);
            skills.Add(new HiscoreSkill
            {
                Id = i,
                Name = mapping.SkillNames[i],
                Rank = ParseInt(parts[0], i),
                Level = ParseInt(parts[1], i),
                Xp = ParseLong(parts[2], i),
            });
        }

        var activities = new List<HiscoreActivity>(mapping.ActivityNames.Count);
        for (var i = 0; i < mapping.ActivityNames.Count; i++)
        {
            var lineIndex = mapping.SkillNames.Count + i;
            if (lineIndex >= lines.Length)
            {
                break;
            }

            var parts = Split(lines[lineIndex], expected: 2, lineIndex);
            activities.Add(new HiscoreActivity
            {
                Id = i,
                Name = mapping.ActivityNames[i],
                Rank = ParseInt(parts[0], lineIndex),
                Score = ParseLong(parts[1], lineIndex),
            });
        }

        return new HiscoreProfile { Skills = skills, Activities = activities };
    }

    /// <summary>Splits a line and checks its field count.</summary>
    /// <param name="line">The line.</param>
    /// <param name="expected">Fields the line should carry.</param>
    /// <param name="lineIndex">Line number, for the error message.</param>
    /// <returns>The fields.</returns>
    private static string[] Split(string line, int expected, int lineIndex)
    {
        var parts = line.Split(',');
        return parts.Length >= expected
            ? parts
            : throw new FormatException($"Hiscore CSV line {lineIndex} has {parts.Length} fields, expected {expected}.");
    }

    /// <summary>Parses an integer field.</summary>
    /// <param name="value">The field.</param>
    /// <param name="lineIndex">Line number, for the error message.</param>
    /// <returns>The value.</returns>
    private static int ParseInt(string value, int lineIndex)
        => int.TryParse(value, NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture, out var parsed)
            ? parsed
            : throw new FormatException($"Hiscore CSV line {lineIndex} carries '{value}' where an integer was expected.");

    /// <summary>Parses a long field.</summary>
    /// <param name="value">The field.</param>
    /// <param name="lineIndex">Line number, for the error message.</param>
    /// <returns>The value.</returns>
    private static long ParseLong(string value, int lineIndex)
        => long.TryParse(value, NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture, out var parsed)
            ? parsed
            : throw new FormatException($"Hiscore CSV line {lineIndex} carries '{value}' where a number was expected.");
}
