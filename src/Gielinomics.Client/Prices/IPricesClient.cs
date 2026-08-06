namespace Gielinomics.Client.Prices;

/// <summary>
/// Typed access to the OSRS wiki real-time prices API (<c>prices.runescape.wiki/api/v2/osrs</c>).
/// </summary>
/// <remarks>
/// Data served by this API is licensed CC BY-NC-SA 3.0. Attribute the wiki, and do not
/// build a commercial product on top of it.
/// </remarks>
public interface IPricesClient
{
    /// <summary>
    /// Fetches the most recent trade for every item in one call.
    /// </summary>
    /// <remarks>
    /// <b>Never loop this per item.</b> The wiki explicitly asks callers not to, and one bulk
    /// call covers all ~3700 items. <see cref="GetLatestAsync(int, CancellationToken)"/> exists
    /// for the genuinely single-item case only.
    /// </remarks>
    /// <param name="cancellationToken">Cancels the request.</param>
    /// <returns>Item ID to its most recent observed trade.</returns>
    Task<IReadOnlyDictionary<int, LatestPrice>> GetLatestAsync(CancellationToken cancellationToken = default);

    /// <summary>Fetches the most recent trade for a single item.</summary>
    /// <param name="itemId">The item's game ID.</param>
    /// <param name="cancellationToken">Cancels the request.</param>
    /// <returns>The item's latest trade, or null if the API returned no row for it.</returns>
    Task<LatestPrice?> GetLatestAsync(int itemId, CancellationToken cancellationToken = default);

    /// <summary>Fetches static reference metadata for every tradeable item.</summary>
    /// <param name="cancellationToken">Cancels the request.</param>
    /// <returns>All item mappings.</returns>
    Task<IReadOnlyList<ItemMapping>> GetMappingAsync(CancellationToken cancellationToken = default);

    /// <summary>Fetches the 5-minute aggregate window.</summary>
    /// <param name="timestamp">
    /// Start of the window to fetch. When null the API returns the most recently completed window.
    /// Pass an explicit value to repair a detected gap.
    /// </param>
    /// <param name="cancellationToken">Cancels the request.</param>
    /// <returns>The window's bars keyed by item ID, and the window start the server actually served.</returns>
    Task<PriceEnvelope<PriceBar>> Get5mAsync(DateTimeOffset? timestamp = null, CancellationToken cancellationToken = default);

    /// <summary>Fetches the 1-hour aggregate window.</summary>
    /// <param name="timestamp">
    /// Start of the window to fetch. When null the API returns the most recently completed window.
    /// </param>
    /// <param name="cancellationToken">Cancels the request.</param>
    /// <returns>The window's bars keyed by item ID, and the window start the server actually served.</returns>
    Task<PriceEnvelope<PriceBar>> Get1hAsync(DateTimeOffset? timestamp = null, CancellationToken cancellationToken = default);

    /// <summary>
    /// Fetches a historical series for one item.
    /// </summary>
    /// <remarks>
    /// The returned granularity depends on <paramref name="lookback"/> — a year of lookback
    /// yields daily bars. Read <see cref="TimeSeriesResponse.TimeStep"/> to find out what you got.
    /// </remarks>
    /// <param name="itemId">The item's game ID.</param>
    /// <param name="lookback">How far back to reach.</param>
    /// <param name="cancellationToken">Cancels the request.</param>
    /// <returns>The series, with the server-reported step.</returns>
    Task<TimeSeriesResponse> GetTimeSeriesAsync(int itemId, Lookback lookback, CancellationToken cancellationToken = default);
}
