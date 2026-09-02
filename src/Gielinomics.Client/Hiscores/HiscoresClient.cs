using System.Net;
using System.Text.Json;
using Gielinomics.Client.Json;
using Microsoft.Extensions.Logging;

namespace Gielinomics.Client.Hiscores;

/// <inheritdoc cref="IHiscoresClient"/>
/// <param name="http">Must have a base address and a descriptive User-Agent set.</param>
/// <param name="logger">Log sink, used for the schema drift alarm.</param>
public sealed class HiscoresClient(HttpClient http, ILogger<HiscoresClient> logger) : IHiscoresClient
{
    private readonly HttpClient _http = http ?? throw new ArgumentNullException(nameof(http));
    private readonly ILogger<HiscoresClient> _logger = logger ?? throw new ArgumentNullException(nameof(logger));

    /// <inheritdoc />
    public async Task<HiscoreProfile?> GetAsync(
        string player,
        HiscoreTable table = HiscoreTable.Main,
        CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(player);

        // Names allow spaces and are case-insensitive. Encoding is not optional: an
        // unencoded space produces a malformed request line, not a lenient lookup.
        var uri = $"m={table.ToWireValue()}/index_lite.json?player={Uri.EscapeDataString(player)}";

        using var response = await _http
            .GetAsync(uri, HttpCompletionOption.ResponseHeadersRead, cancellationToken)
            .ConfigureAwait(false);

        // A 404 is "not on this table", which is a legitimate answer and the whole basis of
        // account type detection. It must not be conflated with a transient failure.
        if (response.StatusCode == HttpStatusCode.NotFound)
        {
            return null;
        }

        if (!response.IsSuccessStatusCode)
        {
            throw new HiscoresApiException($"GET {uri} failed with {(int)response.StatusCode} {response.ReasonPhrase}.")
            {
                StatusCode = response.StatusCode,
                Player = player,
                Table = table,
            };
        }

        HiscoreProfile? profile;
        try
        {
            var stream = await response.Content.ReadAsStreamAsync(cancellationToken).ConfigureAwait(false);
            await using (stream.ConfigureAwait(false))
            {
                profile = await JsonSerializer
                    .DeserializeAsync(stream, GielinomicsJsonContext.Default.HiscoreProfile, cancellationToken)
                    .ConfigureAwait(false);
            }
        }
        catch (JsonException ex)
        {
            throw new HiscoresApiException($"GET {uri} returned a body that could not be parsed.", ex)
            {
                StatusCode = response.StatusCode,
                Player = player,
                Table = table,
            };
        }

        if (profile is null)
        {
            throw new HiscoresApiException($"GET {uri} returned a null body.")
            {
                StatusCode = response.StatusCode,
                Player = player,
                Table = table,
            };
        }

        WarnOnSchemaDrift(profile);

        return profile with { Table = table };
    }

    /// <inheritdoc />
    public async Task<HiscoreTable?> DetectAccountTypeAsync(string player, CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(player);

        foreach (var table in HiscoreTables.DetectionOrder)
        {
            var profile = await GetAsync(player, table, cancellationToken).ConfigureAwait(false);
            if (profile is not null)
            {
                return table;
            }
        }

        return null;
    }

    /// <summary>
    /// Logs skills and activities the current mapping does not know about.
    /// </summary>
    /// <remarks>
    /// With the JSON endpoint this is cheap insurance rather than a correctness requirement —
    /// the server names each field, so a new boss cannot misattribute the ones after it. It
    /// still matters: an unknown name means the CSV fallback's positional mapping has gone
    /// stale, and that one <i>would</i> misattribute silently.
    /// </remarks>
    /// <param name="profile">The profile just fetched.</param>
    private void WarnOnSchemaDrift(HiscoreProfile profile)
    {
        var mapping = HiscoreMapping.Current;
        var unknown = new List<string>();

        foreach (var skill in profile.Skills)
        {
            if (!mapping.Knows(skill.Name))
            {
                unknown.Add($"skill:{skill.Name}");
            }
        }

        foreach (var activity in profile.Activities)
        {
            if (!mapping.Knows(activity.Name))
            {
                unknown.Add($"activity:{activity.Name}");
            }
        }

        if (unknown.Count > 0)
        {
            _logger.LogError(
                "Hiscores response carries {Count} entries outside mapping version {Version} — {Names}. Update HiscoreMapping and bump its version; the CSV fallback is misaligned until you do.",
                unknown.Count,
                mapping.Version,
                string.Join(", ", unknown));
        }
    }
}

/// <summary>Thrown when the hiscores API answers with a non-success status other than 404.</summary>
public sealed class HiscoresApiException : Exception
{
    /// <summary>Creates an exception with a message.</summary>
    /// <param name="message">What went wrong.</param>
    public HiscoresApiException(string message)
        : base(message)
    {
    }

    /// <summary>Creates an exception wrapping an underlying failure.</summary>
    /// <param name="message">What went wrong.</param>
    /// <param name="innerException">The failure being wrapped.</param>
    public HiscoresApiException(string message, Exception innerException)
        : base(message, innerException)
    {
    }

    /// <summary>The status returned.</summary>
    public HttpStatusCode? StatusCode { get; init; }

    /// <summary>The player being looked up.</summary>
    public string? Player { get; init; }

    /// <summary>The table being queried.</summary>
    public HiscoreTable? Table { get; init; }
}
