using System.Globalization;

namespace Gielinomics.Api.Infrastructure;

/// <summary>Shared parsing and response conventions for the query API.</summary>
public static class QueryConventions
{
    /// <summary>Granularities the API will serve, keyed by their wire name.</summary>
    public static IReadOnlyDictionary<string, int> Intervals { get; } = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase)
    {
        ["5m"] = 300,
        ["1h"] = 3600,
        ["1d"] = 86_400,
        ["24h"] = 86_400,
    };

    /// <summary>Largest page any list route will return.</summary>
    public const int MaxPageSize = 200;

    /// <summary>Largest number of bars a single price query will return.</summary>
    /// <remarks>
    /// A year of 5-minute bars is 105,120 rows for one item. Serving that in one response is
    /// not a feature, it is an accident waiting to be reported as a performance problem.
    /// </remarks>
    public const int MaxPricePoints = 5_000;

    /// <summary>Resolves an interval name to a step in seconds.</summary>
    /// <param name="interval">The wire name, or null for the 5-minute default.</param>
    /// <param name="stepSeconds">The resolved step.</param>
    /// <returns>True when the name is one the API serves.</returns>
    public static bool TryResolveInterval(string? interval, out int stepSeconds)
    {
        if (string.IsNullOrWhiteSpace(interval))
        {
            stepSeconds = 300;
            return true;
        }

        return Intervals.TryGetValue(interval, out stepSeconds);
    }

    /// <summary>
    /// Parses a lookback window such as <c>24h</c>, <c>7d</c> or <c>90m</c>.
    /// </summary>
    /// <param name="window">The window, or null for the supplied default.</param>
    /// <param name="fallback">Value to use when <paramref name="window"/> is absent.</param>
    /// <param name="value">The parsed window.</param>
    /// <returns>True when the window was absent or well-formed and positive.</returns>
    public static bool TryParseWindow(string? window, TimeSpan fallback, out TimeSpan value)
    {
        if (string.IsNullOrWhiteSpace(window))
        {
            value = fallback;
            return true;
        }

        var span = window.AsSpan().Trim();
        var unit = span[^1];
        var magnitude = span[..^1];

        if (!int.TryParse(magnitude, NumberStyles.None, CultureInfo.InvariantCulture, out var amount) || amount <= 0)
        {
            value = default;
            return false;
        }

        value = char.ToLowerInvariant(unit) switch
        {
            'm' => TimeSpan.FromMinutes(amount),
            'h' => TimeSpan.FromHours(amount),
            'd' => TimeSpan.FromDays(amount),
            'w' => TimeSpan.FromDays(amount * 7),
            _ => TimeSpan.Zero,
        };

        return value > TimeSpan.Zero;
    }

    /// <summary>Clamps a caller-supplied page size into the allowed range.</summary>
    /// <param name="limit">The requested size, or null.</param>
    /// <param name="fallback">Size to use when none was requested.</param>
    /// <returns>A usable page size.</returns>
    public static int ClampPageSize(int? limit, int fallback = 50)
        => limit is null ? fallback : Math.Clamp(limit.Value, 1, MaxPageSize);

    /// <summary>Sets a public cache lifetime on a hot read.</summary>
    /// <param name="response">The response.</param>
    /// <param name="lifetime">How long the answer stays good enough.</param>
    public static void CacheFor(HttpResponse response, TimeSpan lifetime)
    {
        ArgumentNullException.ThrowIfNull(response);

        response.Headers.CacheControl =
            $"public, max-age={(int)lifetime.TotalSeconds}";
    }
}

/// <summary>A page of results with a cursor to the next one.</summary>
/// <typeparam name="T">Element type.</typeparam>
/// <param name="Items">The page.</param>
/// <param name="NextCursor">
/// Cursor for the following page, or null when this was the last. Keyset, not offset: the
/// ingest worker is inserting while a client pages, and an offset would silently skip rows.
/// </param>
public sealed record Page<T>(IReadOnlyList<T> Items, string? NextCursor);
