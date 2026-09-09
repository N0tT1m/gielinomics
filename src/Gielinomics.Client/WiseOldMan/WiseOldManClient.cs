using System.Globalization;
using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization.Metadata;
using Gielinomics.Client.Json;

namespace Gielinomics.Client.WiseOldMan;

/// <inheritdoc cref="IWiseOldManClient"/>
public sealed class WiseOldManClient : IWiseOldManClient
{
    private readonly HttpClient _http;

    /// <summary>Creates a client over a preconfigured <see cref="HttpClient"/>.</summary>
    /// <param name="http">Must have a base address and a descriptive User-Agent set.</param>
    /// <exception cref="ArgumentNullException"><paramref name="http"/> is null.</exception>
    /// <exception cref="InvalidOperationException">No User-Agent is set.</exception>
    public WiseOldManClient(HttpClient http)
    {
        ArgumentNullException.ThrowIfNull(http);

        // Same failure mode as the wiki, and worth failing at construction for the same reason:
        // a request sent as curl/8.0 is answered with 403, verified live. Discovering that as a
        // 403 storm in production is strictly worse than discovering it at startup.
        if (http.DefaultRequestHeaders.UserAgent.Count == 0)
        {
            throw new InvalidOperationException(
                "A descriptive User-Agent is required. Wise Old Man answers default agents with 403.");
        }

        _http = http;
    }

    /// <inheritdoc />
    public Task<WiseOldManPlayer?> GetPlayerAsync(string username, CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(username);

        return GetOrNullAsync(
            $"players/{EncodeUsername(username)}",
            GielinomicsJsonContext.Default.WiseOldManPlayer,
            cancellationToken);
    }

    /// <inheritdoc />
    public Task<WiseOldManGains?> GetPlayerGainsAsync(
        string username,
        WiseOldManPeriod period = WiseOldManPeriod.Week,
        CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(username);

        return GetOrNullAsync(
            $"players/{EncodeUsername(username)}/gained?period={period.ToWireValue()}",
            GielinomicsJsonContext.Default.WiseOldManGains,
            cancellationToken);
    }

    /// <inheritdoc />
    public Task<IReadOnlyList<WiseOldManSnapshot>?> GetPlayerSnapshotsAsync(
        string username,
        WiseOldManPeriod period = WiseOldManPeriod.Week,
        CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(username);

        return GetOrNullAsync(
            $"players/{EncodeUsername(username)}/snapshots?period={period.ToWireValue()}",
            GielinomicsJsonContext.Default.IReadOnlyListWiseOldManSnapshot,
            cancellationToken);
    }

    /// <inheritdoc />
    public Task<WiseOldManGroup?> GetGroupAsync(long groupId, CancellationToken cancellationToken = default)
        => GetOrNullAsync(
            FormattableString.Invariant($"groups/{groupId}"),
            GielinomicsJsonContext.Default.WiseOldManGroup,
            cancellationToken);

    /// <inheritdoc />
    public Task<IReadOnlyList<WiseOldManGroupGains>?> GetGroupGainsAsync(
        long groupId,
        string metric = "overall",
        WiseOldManPeriod period = WiseOldManPeriod.Week,
        int? limit = null,
        int? offset = null,
        CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(metric);

        var uri = new StringBuilder(64)
            .Append(CultureInfo.InvariantCulture, $"groups/{groupId}/gained")
            .Append(CultureInfo.InvariantCulture, $"?metric={Uri.EscapeDataString(metric)}")
            .Append(CultureInfo.InvariantCulture, $"&period={period.ToWireValue()}");

        if (limit is { } l)
        {
            uri.Append(CultureInfo.InvariantCulture, $"&limit={l}");
        }

        if (offset is { } o)
        {
            uri.Append(CultureInfo.InvariantCulture, $"&offset={o}");
        }

        return GetOrNullAsync(
            uri.ToString(),
            GielinomicsJsonContext.Default.IReadOnlyListWiseOldManGroupGains,
            cancellationToken);
    }

    /// <inheritdoc />
    public Task<WiseOldManCompetition?> GetCompetitionAsync(long competitionId, CancellationToken cancellationToken = default)
        => GetOrNullAsync(
            FormattableString.Invariant($"competitions/{competitionId}"),
            GielinomicsJsonContext.Default.WiseOldManCompetition,
            cancellationToken);

    /// <inheritdoc />
    public async Task<IReadOnlyList<WiseOldManEfficiencyRate>> GetEfficiencyRatesAsync(
        string metric = "ehp",
        string accountType = "main",
        CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(metric);
        ArgumentException.ThrowIfNullOrWhiteSpace(accountType);

        var uri = $"efficiency/rates?metric={Uri.EscapeDataString(metric)}&type={Uri.EscapeDataString(accountType)}";

        // No null case: this route has no per-entity 404, so an empty rate set would be a
        // failure to report rather than an absence to hand back.
        var rates = await GetOrNullAsync(
            uri,
            GielinomicsJsonContext.Default.IReadOnlyListWiseOldManEfficiencyRate,
            cancellationToken).ConfigureAwait(false);

        return rates ?? throw new WiseOldManApiException($"GET {uri} returned no rates.")
        {
            RequestUri = uri,
        };
    }

    /// <summary>
    /// Encodes a display name for use as a path segment.
    /// </summary>
    /// <remarks>
    /// Names allow spaces, and the API accepts either a space or an underscore. Encoding is not
    /// optional: an unencoded space produces a malformed request line rather than a lenient
    /// lookup. The server normalises casing itself, so this does not.
    /// </remarks>
    /// <param name="username">The display name.</param>
    /// <returns>The encoded segment.</returns>
    internal static string EncodeUsername(string username) => Uri.EscapeDataString(username.Trim());

    /// <summary>
    /// Issues a GET, mapping a 404 to null and every other failure to an exception.
    /// </summary>
    /// <remarks>
    /// A 404 here means "Wise Old Man does not know this player, group or competition", which
    /// every caller has to handle and none of them want as an exception. It is not evidence the
    /// account does not exist — only the hiscores can answer that.
    /// </remarks>
    /// <typeparam name="T">The response type.</typeparam>
    /// <param name="relativeUri">URI relative to the client's base address.</param>
    /// <param name="typeInfo">The source-generated contract for <typeparamref name="T"/>.</param>
    /// <param name="cancellationToken">Cancels the request.</param>
    /// <returns>The deserialised body, or null on a 404.</returns>
    /// <exception cref="WiseOldManApiException">Non-success status other than 404, or a body that will not parse.</exception>
    private async Task<T?> GetOrNullAsync<T>(
        string relativeUri,
        JsonTypeInfo<T> typeInfo,
        CancellationToken cancellationToken)
    {
        using var request = new HttpRequestMessage(HttpMethod.Get, relativeUri);
        request.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));

        using var response = await _http
            .SendAsync(request, HttpCompletionOption.ResponseHeadersRead, cancellationToken)
            .ConfigureAwait(false);

        if (response.StatusCode == HttpStatusCode.NotFound)
        {
            return default;
        }

        if (!response.IsSuccessStatusCode)
        {
            var error = await ReadErrorAsync(response, cancellationToken).ConfigureAwait(false);

            throw new WiseOldManApiException(
                $"GET {relativeUri} failed with {(int)response.StatusCode} {response.ReasonPhrase}." +
                (error?.Message is { Length: > 0 } m ? $" {m}" : string.Empty))
            {
                StatusCode = response.StatusCode,
                RequestUri = relativeUri,
                ErrorCode = error?.Code,
                ErrorMessage = error?.Message,
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
            throw new WiseOldManApiException($"GET {relativeUri} returned a body that could not be parsed.", ex)
            {
                StatusCode = response.StatusCode,
                RequestUri = relativeUri,
            };
        }

        return value ?? throw new WiseOldManApiException($"GET {relativeUri} returned a null body.")
        {
            StatusCode = response.StatusCode,
            RequestUri = relativeUri,
        };
    }

    /// <summary>
    /// Reads the <c>{"code", "message"}</c> body an error carries, if it has one.
    /// </summary>
    /// <remarks>
    /// Best effort by design. This runs while building an exception that is going to be thrown
    /// regardless, so a Cloudflare HTML error page or a truncated body must not replace the
    /// status code the caller actually needs with a parse failure.
    /// </remarks>
    /// <param name="response">The failed response.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The parsed error, or null if there was not one to parse.</returns>
    private static async Task<WiseOldManError?> ReadErrorAsync(
        HttpResponseMessage response,
        CancellationToken cancellationToken)
    {
        try
        {
            var stream = await response.Content.ReadAsStreamAsync(cancellationToken).ConfigureAwait(false);
            await using (stream.ConfigureAwait(false))
            {
                return await JsonSerializer
                    .DeserializeAsync(stream, GielinomicsJsonContext.Default.WiseOldManError, cancellationToken)
                    .ConfigureAwait(false);
            }
        }
        catch (JsonException)
        {
            return null;
        }
        catch (HttpRequestException)
        {
            return null;
        }
    }
}
