using System.Text.Json.Serialization;

namespace Gielinomics.Client.Prices;

/// <summary>
/// The most recent instant-buy and instant-sell observed for an item.
/// </summary>
/// <remarks>
/// Every member is nullable and the nulls are meaningful: an item that has never
/// had a recorded instant-buy reports <see cref="High"/> and <see cref="HighTime"/>
/// as null. Do not coalesce these to zero — zero is a price, null is an absence.
/// </remarks>
public sealed record LatestPrice
{
    /// <summary>Most recent instant-buy price, or null if never observed.</summary>
    [JsonPropertyName("high")]
    public long? High { get; init; }

    /// <summary>Unix time of the most recent instant-buy, or null if never observed.</summary>
    [JsonPropertyName("highTime")]
    public long? HighTime { get; init; }

    /// <summary>Most recent instant-sell price, or null if never observed.</summary>
    [JsonPropertyName("low")]
    public long? Low { get; init; }

    /// <summary>Unix time of the most recent instant-sell, or null if never observed.</summary>
    [JsonPropertyName("lowTime")]
    public long? LowTime { get; init; }

    /// <summary>The instant-buy timestamp as a <see cref="DateTimeOffset"/>, or null.</summary>
    public DateTimeOffset? HighTimeUtc => HighTime is { } t ? DateTimeOffset.FromUnixTimeSeconds(t) : null;

    /// <summary>The instant-sell timestamp as a <see cref="DateTimeOffset"/>, or null.</summary>
    public DateTimeOffset? LowTimeUtc => LowTime is { } t ? DateTimeOffset.FromUnixTimeSeconds(t) : null;
}

/// <summary>
/// Static reference metadata for a tradeable item, from the <c>/mapping</c> route.
/// </summary>
public sealed record ItemMapping
{
    /// <summary>The item's game ID.</summary>
    [JsonPropertyName("id")]
    public required int Id { get; init; }

    /// <summary>Display name.</summary>
    [JsonPropertyName("name")]
    public required string Name { get; init; }

    /// <summary>In-game examine text.</summary>
    [JsonPropertyName("examine")]
    public string? Examine { get; init; }

    /// <summary>Whether the item is members-only.</summary>
    [JsonPropertyName("members")]
    public bool Members { get; init; }

    /// <summary>Grand Exchange buy limit per 4 hours. Null when the item has no published limit.</summary>
    [JsonPropertyName("limit")]
    public int? Limit { get; init; }

    /// <summary>Item's base store value.</summary>
    [JsonPropertyName("value")]
    public long? Value { get; init; }

    /// <summary>Low alchemy value.</summary>
    [JsonPropertyName("lowalch")]
    public long? LowAlch { get; init; }

    /// <summary>High alchemy value.</summary>
    [JsonPropertyName("highalch")]
    public long? HighAlch { get; init; }

    /// <summary>Icon filename on the wiki.</summary>
    [JsonPropertyName("icon")]
    public string? Icon { get; init; }
}

/// <summary>
/// A volume-weighted price bar over a fixed window, from <c>/5m</c>, <c>/1h</c>, or <c>/timeseries</c>.
/// </summary>
/// <remarks>
/// Averages carry decimals, so these are <see cref="decimal"/> rather than integral.
/// A window in which no trade occurred reports null prices with zero volume.
/// </remarks>
public record PriceBar
{
    /// <summary>Volume-weighted average instant-buy price over the window.</summary>
    [JsonPropertyName("avgHighPrice")]
    public decimal? AvgHighPrice { get; init; }

    /// <summary>Number of units bought at the instant-buy price over the window.</summary>
    [JsonPropertyName("highPriceVolume")]
    public long HighPriceVolume { get; init; }

    /// <summary>Volume-weighted average instant-sell price over the window.</summary>
    [JsonPropertyName("avgLowPrice")]
    public decimal? AvgLowPrice { get; init; }

    /// <summary>Number of units sold at the instant-sell price over the window.</summary>
    [JsonPropertyName("lowPriceVolume")]
    public long LowPriceVolume { get; init; }
}

/// <summary>A <see cref="PriceBar"/> carrying its own window start, as returned by <c>/timeseries</c>.</summary>
public sealed record TimeSeriesPoint : PriceBar
{
    /// <summary>Unix time of the start of this window.</summary>
    [JsonPropertyName("timestamp")]
    public long Timestamp { get; init; }

    /// <summary>The window start as a <see cref="DateTimeOffset"/>.</summary>
    public DateTimeOffset TimestampUtc => DateTimeOffset.FromUnixTimeSeconds(Timestamp);
}

/// <summary>
/// The historical window requested from <c>/timeseries</c>.
/// </summary>
/// <remarks>
/// <para>
/// <b>Granularity is tied to the window, and this is the single most important fact about this route.</b>
/// A <see cref="OneYear"/> lookback returns 365 points at a 86400-second step — daily bars, not
/// 5-minute ones. Fine-grained history simply does not exist upstream beyond the short windows.
/// </para>
/// <para>
/// Always read <see cref="TimeSeriesResponse.TimeStep"/> from the response rather than inferring
/// the step from the lookback you asked for. It is not contractual and has changed before.
/// </para>
/// </remarks>
public enum Lookback
{
    /// <summary>Six hours.</summary>
    SixHours,

    /// <summary>Twenty-four hours.</summary>
    OneDay,

    /// <summary>Seven days.</summary>
    OneWeek,

    /// <summary>Thirty days.</summary>
    OneMonth,

    /// <summary>Six months.</summary>
    SixMonths,

    /// <summary>One year. Returns daily bars.</summary>
    OneYear,
}

/// <summary>The <c>/timeseries</c> response envelope.</summary>
public sealed record TimeSeriesResponse
{
    /// <summary>The ordered series.</summary>
    [JsonPropertyName("data")]
    public required IReadOnlyList<TimeSeriesPoint> Data { get; init; }

    /// <summary>The item this series describes.</summary>
    [JsonPropertyName("itemId")]
    public int ItemId { get; init; }

    /// <summary>Unix time of the first window.</summary>
    [JsonPropertyName("startTimestamp")]
    public long StartTimestamp { get; init; }

    /// <summary>Unix time of the last window.</summary>
    [JsonPropertyName("endTimestamp")]
    public long EndTimestamp { get; init; }

    /// <summary>
    /// Width of each window in seconds, as reported by the server. Persist this alongside
    /// the rows; never assume it from the requested <see cref="Lookback"/>.
    /// </summary>
    [JsonPropertyName("timestep")]
    public int TimeStep { get; init; }
}

/// <summary>Envelope for the routes that return a map of item ID to payload.</summary>
/// <typeparam name="T">The per-item payload type.</typeparam>
public sealed record PriceEnvelope<T>
{
    /// <summary>Item ID to payload.</summary>
    [JsonPropertyName("data")]
    public required IReadOnlyDictionary<int, T> Data { get; init; }

    /// <summary>Unix time of the window these rows describe. Absent on <c>/latest</c>.</summary>
    [JsonPropertyName("timestamp")]
    public long? Timestamp { get; init; }
}
