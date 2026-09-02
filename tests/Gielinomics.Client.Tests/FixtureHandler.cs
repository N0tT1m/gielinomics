using System.Net;
using System.Text;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;

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

    private Func<Uri?, bool>? _serveWhen;

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

    /// <summary>Every URI requested, in order. Account type detection asserts on this.</summary>
    public List<Uri> Requests { get; } = [];

    /// <summary>
    /// Serves the body only for URIs matching a predicate; everything else gets a 404.
    /// </summary>
    /// <remarks>
    /// The hiscores API answers 404 for "this player is not on this table", which is the whole
    /// basis of account type detection. Modelling that needs per-request behaviour.
    /// </remarks>
    /// <param name="predicate">Which requests should succeed.</param>
    /// <returns>This handler, for chaining.</returns>
    public FixtureHandler ServeOnlyWhen(Func<Uri?, bool> predicate)
    {
        _serveWhen = predicate;
        return this;
    }

    /// <summary>Builds a hiscores client wired to this handler.</summary>
    /// <param name="logger">Log sink, so tests can assert on the schema drift alarm.</param>
    /// <returns>The client under test.</returns>
    public Hiscores.HiscoresClient CreateHiscoresClient(ILogger<Hiscores.HiscoresClient>? logger = null)
    {
        var http = new HttpClient(this, disposeHandler: false)
        {
            BaseAddress = new Uri("https://secure.runescape.com/"),
        };

        http.DefaultRequestHeaders.UserAgent.ParseAdd("gielinomics-tests/0.1 (github.com/N0tT1m/gielinomics)");
        return new Hiscores.HiscoresClient(http, logger ?? NullLogger<Hiscores.HiscoresClient>.Instance);
    }

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
        if (request.RequestUri is not null)
        {
            Requests.Add(request.RequestUri);
        }

        if (_serveWhen is not null && !_serveWhen(request.RequestUri))
        {
            return Task.FromResult(new HttpResponseMessage(HttpStatusCode.NotFound)
            {
                Content = new StringContent("<html>404</html>", Encoding.UTF8, "text/html"),
                RequestMessage = request,
            });
        }

        return Task.FromResult(new HttpResponseMessage(_statusCode)
        {
            Content = new StringContent(_body, Encoding.UTF8, _contentType),
            RequestMessage = request,
        });
    }
}
