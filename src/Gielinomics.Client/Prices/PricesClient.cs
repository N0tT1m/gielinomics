using System.Globalization;
using System.Net.Http.Headers;
using System.Text.Json;
using System.Text.Json.Serialization.Metadata;
using Gielinomics.Client.Json;

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
    public async Task<IReadOnlyDictionary<int, LatestPrice>> GetLatestAsync(CancellationToken cancellationToken = default)
    {
        var envelope = await GetAsync("latest", GielinomicsJsonContext.Default.PriceEnvelopeLatestPrice, cancellationToken)
            .ConfigureAwait(false);

        return envelope.Data;
    }

    /// <inheritdoc />
    public async Task<LatestPrice?> GetLatestAsync(int itemId, CancellationToken cancellationToken = default)
    {
        var uri = FormattableString.Invariant($"latest?id={itemId}");
        var envelope = await GetAsync(uri, GielinomicsJsonContext.Default.PriceEnvelopeLatestPrice, cancellationToken)
            .ConfigureAwait(false);

        return envelope.Data.TryGetValue(itemId, out var price) ? price : null;
    }

    /// <inheritdoc />
    public Task<IReadOnlyList<ItemMapping>> GetMappingAsync(CancellationToken cancellationToken = default)
        => GetAsync("mapping", GielinomicsJsonContext.Default.IReadOnlyListItemMapping, cancellationToken);

    /// <inheritdoc />
    public Task<PriceEnvelope<PriceBar>> Get5mAsync(DateTimeOffset? timestamp = null, CancellationToken cancellationToken = default)
        => GetAsync(BuildWindowUri("5m", timestamp), GielinomicsJsonContext.Default.PriceEnvelopePriceBar, cancellationToken);

    /// <inheritdoc />
    public Task<PriceEnvelope<PriceBar>> Get1hAsync(DateTimeOffset? timestamp = null, CancellationToken cancellationToken = default)
        => GetAsync(BuildWindowUri("1h", timestamp), GielinomicsJsonContext.Default.PriceEnvelopePriceBar, cancellationToken);

    /// <inheritdoc />
    public Task<TimeSeriesResponse> GetTimeSeriesAsync(int itemId, Lookback lookback, CancellationToken cancellationToken = default)
    {
        var uri = FormattableString.Invariant($"timeseries?id={itemId}&lookback={ToWireValue(lookback)}");
        return GetAsync(uri, GielinomicsJsonContext.Default.TimeSeriesResponse, cancellationToken);
    }

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

    /// <summary>
    /// Builds the relative URI for an aggregate window route.
    /// </summary>
    /// <remarks>
    /// Omitting <paramref name="timestamp"/> is not the same as passing "now": the API
    /// answers a bare call with the most recently <i>completed</i> window, which is what
    /// the poll wants. Gap repair passes an explicit window instead.
    /// </remarks>
    /// <param name="route">The route name, <c>5m</c> or <c>1h</c>.</param>
    /// <param name="timestamp">Explicit window start, or null for the newest completed window.</param>
    /// <returns>The relative URI.</returns>
    internal static string BuildWindowUri(string route, DateTimeOffset? timestamp)
        => timestamp is { } ts
            ? string.Create(CultureInfo.InvariantCulture, $"{route}?timestamp={ts.ToUnixTimeSeconds()}")
            : route;

    /// <summary>Issues a GET and deserialises the body with a source-generated contract.</summary>
    /// <typeparam name="T">The response type.</typeparam>
    /// <param name="relativeUri">URI relative to the client's base address.</param>
    /// <param name="typeInfo">The source-generated contract for <typeparamref name="T"/>.</param>
    /// <param name="cancellationToken">Cancels the request.</param>
    /// <returns>The deserialised body.</returns>
    /// <exception cref="PricesApiException">Non-success status, or a body that will not parse.</exception>
    private async Task<T> GetAsync<T>(string relativeUri, JsonTypeInfo<T> typeInfo, CancellationToken cancellationToken)
    {
        using var request = new HttpRequestMessage(HttpMethod.Get, relativeUri);
        request.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));

        using var response = await _http
            .SendAsync(request, HttpCompletionOption.ResponseHeadersRead, cancellationToken)
            .ConfigureAwait(false);

        if (!response.IsSuccessStatusCode)
        {
            throw new PricesApiException(
                $"GET {relativeUri} failed with {(int)response.StatusCode} {response.ReasonPhrase}.")
            {
                StatusCode = response.StatusCode,
                RequestUri = relativeUri,
            };
        }

        T? value;
        try
        {
            var stream = await response.Content.ReadAsStreamAsync(cancellationToken).ConfigureAwait(false);
            await using (stream.ConfigureAwait(false))
            {
                value = await JsonSerializer.DeserializeAsync(stream, typeInfo, cancellationToken).ConfigureAwait(false);
            }
        }
        catch (JsonException ex)
        {
            throw new PricesApiException($"GET {relativeUri} returned a body that could not be parsed.", ex)
            {
                StatusCode = response.StatusCode,
                RequestUri = relativeUri,
            };
        }

        return value ?? throw new PricesApiException($"GET {relativeUri} returned a null body.")
        {
            StatusCode = response.StatusCode,
            RequestUri = relativeUri,
        };
    }
}
