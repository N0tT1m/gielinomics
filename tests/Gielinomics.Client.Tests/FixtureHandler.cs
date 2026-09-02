using System.Net;
using System.Text;

namespace Gielinomics.Client.Tests;

/// <summary>
/// Serves a recorded body instead of making a request, and records what was asked for.
/// </summary>
/// <remarks>
/// No network in CI. The suite must not depend on the wiki being reachable or on any item
/// being any particular price — a test that fails because someone bought a whip is noise.
/// </remarks>
internal sealed class FixtureHandler : HttpMessageHandler
{
    private readonly string _body;
    private readonly HttpStatusCode _statusCode;
    private readonly string _contentType;

    private FixtureHandler(string body, HttpStatusCode statusCode, string contentType)
    {
        _body = body;
        _statusCode = statusCode;
        _contentType = contentType;
    }

    /// <summary>The URI of the most recent request, for asserting on query strings.</summary>
    public Uri? LastRequestUri { get; private set; }

    /// <summary>How many requests have been made through this handler.</summary>
    public int RequestCount { get; private set; }

    /// <summary>Serves a recorded body from <c>Fixtures/</c>.</summary>
    /// <param name="fileName">File name within the fixtures directory.</param>
    /// <returns>The handler.</returns>
    public static FixtureHandler FromFixture(string fileName)
    {
        var path = Path.Combine(AppContext.BaseDirectory, "Fixtures", fileName);
        return new FixtureHandler(File.ReadAllText(path), HttpStatusCode.OK, "application/json");
    }

    /// <summary>Serves an arbitrary body, for malformed-response and error-status cases.</summary>
    /// <param name="body">The body to serve.</param>
    /// <param name="statusCode">The status to serve it with.</param>
    /// <param name="contentType">The content type to claim.</param>
    /// <returns>The handler.</returns>
    public static FixtureHandler FromBody(
        string body,
        HttpStatusCode statusCode = HttpStatusCode.OK,
        string contentType = "application/json")
        => new(body, statusCode, contentType);

    /// <summary>Builds a client wired to this handler, with a User-Agent set.</summary>
    /// <returns>The client under test.</returns>
    public Prices.PricesClient CreateClient()
    {
        var http = new HttpClient(this, disposeHandler: false)
        {
            BaseAddress = new Uri("https://prices.runescape.wiki/api/v2/osrs/"),
        };

        http.DefaultRequestHeaders.UserAgent.ParseAdd("gielinomics-tests/0.1 (github.com/N0tT1m/gielinomics)");
        return new Prices.PricesClient(http);
    }

    /// <inheritdoc />
    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
    {
        LastRequestUri = request.RequestUri;
        RequestCount++;

        return Task.FromResult(new HttpResponseMessage(_statusCode)
        {
            Content = new StringContent(_body, Encoding.UTF8, _contentType),
            RequestMessage = request,
        });
    }
}
