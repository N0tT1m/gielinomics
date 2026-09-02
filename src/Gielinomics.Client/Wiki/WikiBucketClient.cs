using System.Net;
using System.Runtime.CompilerServices;
using System.Text.Json;
using Gielinomics.Client.Json;

namespace Gielinomics.Client.Wiki;

/// <inheritdoc cref="IWikiBucketClient"/>
/// <param name="http">Must have a base address and a descriptive User-Agent set.</param>
public sealed class WikiBucketClient(HttpClient http) : IWikiBucketClient
{
    private readonly HttpClient _http = http ?? throw new ArgumentNullException(nameof(http));

    /// <inheritdoc />
    public async Task<IReadOnlyList<T>> QueryAsync<T>(BucketQuery query, CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(query);

        var rendered = query.ToString();
        var uri = $"api.php?action=bucket&format=json&query={Uri.EscapeDataString(rendered)}";

        using var response = await _http
            .GetAsync(uri, HttpCompletionOption.ResponseHeadersRead, cancellationToken)
            .ConfigureAwait(false);

        if (!response.IsSuccessStatusCode)
        {
            throw new WikiApiException($"Bucket query failed with {(int)response.StatusCode} {response.ReasonPhrase}.")
            {
                StatusCode = response.StatusCode,
                Query = rendered,
            };
        }

        // Source-generated contracts only. The envelope is generic, so the contract is looked
        // up by type rather than named — an unregistered row type fails here, loudly, instead
        // of silently falling back to reflection and breaking under trimming.
        var typeInfo = GielinomicsJsonContext.Default.GetTypeInfo(typeof(BucketEnvelope<T>))
            ?? throw new WikiApiException(
                $"BucketEnvelope<{typeof(T).Name}> is not registered in {nameof(GielinomicsJsonContext)}.")
            {
                Query = rendered,
            };

        BucketEnvelope<T>? envelope;
        try
        {
            var stream = await response.Content.ReadAsStreamAsync(cancellationToken).ConfigureAwait(false);
            await using (stream.ConfigureAwait(false))
            {
                envelope = (BucketEnvelope<T>?)await JsonSerializer
                    .DeserializeAsync(stream, typeInfo, cancellationToken)
                    .ConfigureAwait(false);
            }
        }
        catch (JsonException ex)
        {
            throw new WikiApiException("Bucket returned a body that could not be parsed.", ex)
            {
                StatusCode = response.StatusCode,
                Query = rendered,
            };
        }

        if (envelope is null)
        {
            throw new WikiApiException("Bucket returned a null body.") { Query = rendered };
        }

        // Bucket reports a bad query with HTTP 200 and an error field. Checking only the status
        // would read a Lua syntax error as a successful sync that happened to find nothing.
        if (envelope.Error is { Length: > 0 } error)
        {
            throw new WikiApiException($"Bucket rejected the query: {error}") { Query = rendered };
        }

        return envelope.Bucket ?? [];
    }

    /// <inheritdoc />
    public async IAsyncEnumerable<T> StreamAsync<T>(
        Func<BucketQuery> build,
        string orderBy,
        int pageSize = 5000,
        [EnumeratorCancellation] CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(build);
        ArgumentException.ThrowIfNullOrWhiteSpace(orderBy);
        ArgumentOutOfRangeException.ThrowIfLessThan(pageSize, 1);

        var offset = 0;

        while (!cancellationToken.IsCancellationRequested)
        {
            var page = await QueryAsync<T>(
                build().OrderBy(orderBy).Limit(pageSize).Offset(offset),
                cancellationToken).ConfigureAwait(false);

            foreach (var row in page)
            {
                yield return row;
            }

            // A short page is the end. Asking for one more would cost a request per sync to
            // learn nothing.
            if (page.Count < pageSize) yield break;

            offset += pageSize;
        }
    }
}

/// <summary>Thrown when a Bucket query fails, whether by status or by the API's own error field.</summary>
public sealed class WikiApiException : Exception
{
    /// <summary>Creates an exception with a message.</summary>
    /// <param name="message">What went wrong.</param>
    public WikiApiException(string message)
        : base(message)
    {
    }

    /// <summary>Creates an exception wrapping an underlying failure.</summary>
    /// <param name="message">What went wrong.</param>
    /// <param name="innerException">The failure being wrapped.</param>
    public WikiApiException(string message, Exception innerException)
        : base(message, innerException)
    {
    }

    /// <summary>The status returned, when the failure was an HTTP status.</summary>
    public HttpStatusCode? StatusCode { get; init; }

    /// <summary>The query that failed.</summary>
    public string? Query { get; init; }
}
