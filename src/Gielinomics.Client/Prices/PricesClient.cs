namespace Gielinomics.Client.Prices;

/// <inheritdoc cref="IPricesClient"/>
public sealed class PricesClient : IPricesClient
{
    private readonly HttpClient _http;

    /// <summary>Creates a client over a preconfigured <see cref="HttpClient"/>.</summary>
    /// <param name="http">Must have a base address and a descriptive User-Agent set.</param>
    /// <exception cref="InvalidOperationException">No User-Agent is set.</exception>
    public PricesClient(HttpClient http)
    {
        ArgumentNullException.ThrowIfNull(http);

        // Fail at construction, not in production: the wiki blocks default agents
        // (RestSharp, python-requests, curl) so an unset UA means every call 403s.
        if (http.DefaultRequestHeaders.UserAgent.Count == 0)
        {
            throw new InvalidOperationException(
                "A descriptive User-Agent is required. The OSRS wiki blocks default agents outright.");
        }

        _http = http;
    }

    /// <inheritdoc />
    public Task<IReadOnlyDictionary<int, LatestPrice>> GetLatestAsync(CancellationToken cancellationToken = default)
        => throw new NotImplementedException("TODO: GET latest -> PriceEnvelope<LatestPrice>.Data");

    /// <inheritdoc />
    public Task<LatestPrice?> GetLatestAsync(int itemId, CancellationToken cancellationToken = default)
        => throw new NotImplementedException("TODO: GET latest?id={itemId}. Single item only -- never loop this.");

    /// <inheritdoc />
    public Task<IReadOnlyList<ItemMapping>> GetMappingAsync(CancellationToken cancellationToken = default)
        => throw new NotImplementedException("TODO: GET mapping");

    /// <inheritdoc />
    public Task<PriceEnvelope<PriceBar>> Get5mAsync(DateTimeOffset? timestamp = null, CancellationToken cancellationToken = default)
        => throw new NotImplementedException("TODO: GET 5m[?timestamp=]. The timestamp param is what gap repair uses.");

    /// <inheritdoc />
    public Task<PriceEnvelope<PriceBar>> Get1hAsync(DateTimeOffset? timestamp = null, CancellationToken cancellationToken = default)
        => throw new NotImplementedException("TODO: GET 1h[?timestamp=]");

    /// <inheritdoc />
    public Task<TimeSeriesResponse> GetTimeSeriesAsync(int itemId, Lookback lookback, CancellationToken cancellationToken = default)
        => throw new NotImplementedException("TODO: GET timeseries?id=&lookback=. Persist the server's timestep, not the implied one.");

    /// <summary>Maps a <see cref="Lookback"/> to its wire value.</summary>
    /// <param name="lookback">The window.</param>
    /// <returns>The value the API expects.</returns>
    internal static string ToWireValue(Lookback lookback) => lookback switch
    {
        Lookback.SixHours => "6h",
        Lookback.OneDay => "24h",
        Lookback.OneWeek => "7d",
        Lookback.OneMonth => "30d",
        Lookback.SixMonths => "6m",
        Lookback.OneYear => "1y",
        _ => throw new ArgumentOutOfRangeException(nameof(lookback), lookback, "Unknown lookback window."),
    };
}
