using System.Text.Json;
using System.Text.Json.Serialization;

namespace Gielinomics.Alerts;

/// <summary>The rule kinds the evaluator understands.</summary>
/// <remarks>
/// These are the values stored in <c>alert_rules.kind</c>. <c>xp_milestone</c> is named in
/// the schema but not listed here: it depends on account tracking, which is an unresolved
/// scope decision, and a rule kind the evaluator silently ignores is worse than one that
/// was never accepted at write time.
/// </remarks>
public static class AlertRuleKind
{
    /// <summary>Fires when an item's tax-adjusted flip margin clears a threshold.</summary>
    public const string Margin = "margin";

    /// <summary>Fires when an item's traded volume over a window clears a threshold.</summary>
    public const string Volume = "volume";

    /// <summary>Every kind the evaluator can act on.</summary>
    public static IReadOnlySet<string> All { get; } = new HashSet<string>(StringComparer.Ordinal) { Margin, Volume };
}

/// <summary>Configuration for a <see cref="AlertRuleKind.Margin"/> rule.</summary>
public sealed record MarginRuleConfig
{
    /// <summary>The item to watch.</summary>
    [JsonPropertyName("itemId")]
    public required int ItemId { get; init; }

    /// <summary>Net profit per unit, after tax, that must be reached to fire.</summary>
    [JsonPropertyName("minNetMargin")]
    public required long MinNetMargin { get; init; }

    /// <summary>
    /// Minimum volume on the thinner side of the book over the last day.
    /// </summary>
    /// <remarks>
    /// Zero would make every dead item with a stale wide spread look like a flip. The
    /// evaluator measures the thinner side because a margin you cannot exit is not a margin.
    /// </remarks>
    [JsonPropertyName("minVolume")]
    public long MinVolume { get; init; }
}

/// <summary>Configuration for a <see cref="AlertRuleKind.Volume"/> rule.</summary>
public sealed record VolumeRuleConfig
{
    /// <summary>The item to watch.</summary>
    [JsonPropertyName("itemId")]
    public required int ItemId { get; init; }

    /// <summary>Total traded units over the window that must be reached to fire.</summary>
    [JsonPropertyName("minVolume")]
    public required long MinVolume { get; init; }

    /// <summary>Length of the window in hours.</summary>
    [JsonPropertyName("windowHours")]
    public int WindowHours { get; init; } = 24;
}

/// <summary>Parsing and validation for the JSON stored in <c>alert_rules.config</c>.</summary>
public static class AlertRuleConfig
{
    /// <summary>Serialisation settings for rule configuration.</summary>
    public static JsonSerializerOptions SerializerOptions { get; } = new(JsonSerializerDefaults.Web);

    /// <summary>
    /// Checks that a rule's configuration parses and is internally sensible.
    /// </summary>
    /// <remarks>
    /// Called at write time. A rule that cannot be evaluated must be rejected by the POST
    /// rather than discovered by the dispatcher hours later, where nobody is watching.
    /// </remarks>
    /// <param name="kind">The rule kind.</param>
    /// <param name="configJson">The candidate configuration.</param>
    /// <param name="error">Why it was rejected, when it was.</param>
    /// <returns>True when the configuration is usable.</returns>
    public static bool TryValidate(string kind, string configJson, out string? error)
    {
        if (!AlertRuleKind.All.Contains(kind))
        {
            error = $"Unknown rule kind '{kind}'. Supported kinds: {string.Join(", ", AlertRuleKind.All)}.";
            return false;
        }

        try
        {
            switch (kind)
            {
                case AlertRuleKind.Margin:
                {
                    var config = JsonSerializer.Deserialize<MarginRuleConfig>(configJson, SerializerOptions);
                    if (config is null)
                    {
                        error = "config must be a JSON object.";
                        return false;
                    }

                    if (config.MinNetMargin <= 0)
                    {
                        error = "minNetMargin must be greater than zero.";
                        return false;
                    }

                    if (config.MinVolume < 0)
                    {
                        error = "minVolume cannot be negative.";
                        return false;
                    }

                    break;
                }

                case AlertRuleKind.Volume:
                {
                    var config = JsonSerializer.Deserialize<VolumeRuleConfig>(configJson, SerializerOptions);
                    if (config is null)
                    {
                        error = "config must be a JSON object.";
                        return false;
                    }

                    if (config.MinVolume <= 0)
                    {
                        error = "minVolume must be greater than zero.";
                        return false;
                    }

                    if (config.WindowHours is < 1 or > 168)
                    {
                        error = "windowHours must be between 1 and 168.";
                        return false;
                    }

                    break;
                }
            }
        }
        catch (JsonException ex)
        {
            error = $"config is not valid JSON for a '{kind}' rule: {ex.Message}";
            return false;
        }

        error = null;
        return true;
    }
}
