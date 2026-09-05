using System.Net;

namespace Gielinomics.Alerts.Tests;

/// <summary>
/// An <see cref="HttpMessageHandler"/> that records what was sent and replies with a canned outcome.
/// </summary>
/// <remarks>
/// The dispatcher's whole job is deciding whether a request leaves the box at all, so the
/// interesting assertion is usually that <see cref="Requests"/> is empty.
/// </remarks>
internal sealed class RecordingHandler : HttpMessageHandler
{
    private readonly Func<HttpRequestMessage, HttpResponseMessage> _respond;

    /// <summary>Replies with the given status to every request.</summary>
    /// <param name="status">The status to reply with.</param>
    public RecordingHandler(HttpStatusCode status = HttpStatusCode.NoContent)
        : this(_ => new HttpResponseMessage(status))
    {
    }

    /// <summary>Replies using a caller-supplied function.</summary>
    /// <param name="respond">Produces the reply, or throws to simulate a transport failure.</param>
    public RecordingHandler(Func<HttpRequestMessage, HttpResponseMessage> respond) => _respond = respond;

    /// <summary>Every request the handler was asked to send, with its body already read.</summary>
    public List<(Uri? Uri, string Body)> Requests { get; } = [];

    /// <inheritdoc />
    protected override async Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request,
        CancellationToken cancellationToken)
    {
        var body = request.Content is null
            ? string.Empty
            : await request.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);

        Requests.Add((request.RequestUri, body));
        return _respond(request);
    }
}
