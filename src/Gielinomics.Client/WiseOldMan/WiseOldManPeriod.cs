namespace Gielinomics.Client.WiseOldMan;

/// <summary>
/// A rolling window for gains and snapshot queries.
/// </summary>
/// <remarks>
/// These are the only values the API accepts; anything else comes back as a
/// <c>400</c> with <c>"Invalid period: ..."</c>, verified live. Windows roll backwards from
/// now — there is no way to ask for "last calendar week" through this parameter.
/// </remarks>
public enum WiseOldManPeriod
{
    /// <summary>The last five minutes. Almost always empty; useful only right after an update.</summary>
    FiveMinutes,

    /// <summary>The last 24 hours.</summary>
    Day,

    /// <summary>The last 7 days.</summary>
    Week,

    /// <summary>The last 30 days.</summary>
    Month,

    /// <summary>The last 365 days.</summary>
    Year,
}

/// <summary>Wire names for <see cref="WiseOldManPeriod"/>.</summary>
public static class WiseOldManPeriods
{
    /// <summary>Maps a period to its <c>period=</c> query value.</summary>
    /// <param name="period">The window.</param>
    /// <returns>The wire value.</returns>
    /// <exception cref="ArgumentOutOfRangeException">The period is not one of the known values.</exception>
    public static string ToWireValue(this WiseOldManPeriod period) => period switch
    {
        WiseOldManPeriod.FiveMinutes => "five_min",
        WiseOldManPeriod.Day => "day",
        WiseOldManPeriod.Week => "week",
        WiseOldManPeriod.Month => "month",
        WiseOldManPeriod.Year => "year",
        _ => throw new ArgumentOutOfRangeException(nameof(period), period, "Unknown period."),
    };
}
