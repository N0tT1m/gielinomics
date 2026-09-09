using System.Net;

namespace Gielinomics.Client.WiseOldMan;

/// <summary>
/// Thrown when Wise Old Man answers with a non-success status other than a 404 the route
/// treats as a result, or with a body that will not parse.
/// </summary>
/// <remarks>
/// Distinct from <see cref="Prices.PricesApiException"/> and
/// <see cref="Hiscores.HiscoresApiException"/> on purpose: the three upstreams have separate
/// rate limit budgets, so a caller backing off must be able to tell which one asked it to.
/// </remarks>
public sealed class WiseOldManApiException : Exception
{
    /// <summary>Creates an exception with a message.</summary>
    /// <param name="message">What went wrong.</param>
    public WiseOldManApiException(string message)
        : base(message)
    {
    }

    /// <summary>Creates an exception wrapping an underlying failure.</summary>
    /// <param name="message">What went wrong.</param>
    /// <param name="innerException">The failure being wrapped.</param>
    public WiseOldManApiException(string message, Exception innerException)
        : base(message, innerException)
    {
    }

    /// <summary>The status returned, or null when the failure was not an HTTP status.</summary>
    public HttpStatusCode? StatusCode { get; init; }

    /// <summary>The request URI that failed, relative to the client's base address.</summary>
    public string? RequestUri { get; init; }

    /// <summary>
    /// The <c>code</c> field from the error body, e.g. <c>PLAYER_NOT_FOUND</c>, when there was one.
    /// </summary>
    /// <remarks>
    /// Wise Old Man returns <c>{"code": ..., "message": ...}</c> on failure. The code is worth
    /// surfacing separately: the message is prose that can be reworded without notice, and
    /// branching on prose is how a caller ends up handling an error it stops recognising.
    /// </remarks>
    public string? ErrorCode { get; init; }

    /// <summary>The <c>message</c> field from the error body, when there was one.</summary>
    public string? ErrorMessage { get; init; }

    /// <summary>
    /// Whether retrying this request could plausibly succeed.
    /// </summary>
    /// <remarks>
    /// 429 is the one that matters here. Wise Old Man allows 20 requests per 60 seconds without
    /// an API key — verified live from the <c>ratelimit-limit</c> response header — which is
    /// tight enough that any sweep over a group will meet it. Back off on this rather than
    /// treating it as a failure.
    /// </remarks>
    public bool IsTransient => StatusCode switch
    {
        null => false,
        HttpStatusCode.NotFound => false,
        HttpStatusCode.BadRequest => false,
        HttpStatusCode.Forbidden => false,
        HttpStatusCode.TooManyRequests => true,
        HttpStatusCode.RequestTimeout => true,
        var code => (int)code >= 500,
    };
}
