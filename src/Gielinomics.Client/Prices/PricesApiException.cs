using System.Net;

namespace Gielinomics.Client.Prices;

/// <summary>
/// Thrown when the prices API answers with a non-success status, or with a body that
/// cannot be read as the shape the route documents.
/// </summary>
/// <remarks>
/// Deliberately distinct from <see cref="HttpRequestException"/>. A caller retrying on
/// transport faults wants to treat "the wiki returned 429" differently from "the wiki
/// returned 200 and something that is not JSON", and the resilience handler in the worker
/// keys off status codes that only this type carries.
/// </remarks>
public sealed class PricesApiException : Exception
{
    /// <summary>Creates an exception with a message.</summary>
    /// <param name="message">What went wrong.</param>
    public PricesApiException(string message)
        : base(message)
    {
    }

    /// <summary>Creates an exception wrapping an underlying failure.</summary>
    /// <param name="message">What went wrong.</param>
    /// <param name="innerException">The failure being wrapped.</param>
    public PricesApiException(string message, Exception innerException)
        : base(message, innerException)
    {
    }

    /// <summary>The status code returned, or null when the failure was not an HTTP status.</summary>
    public HttpStatusCode? StatusCode { get; init; }

    /// <summary>The request URI that failed, relative to the client's base address.</summary>
    public string? RequestUri { get; init; }

    /// <summary>
    /// Whether retrying this request could plausibly succeed.
    /// </summary>
    /// <remarks>
    /// 404 is excluded on purpose: the plan calls out not retrying it. A missing item is
    /// missing on the next attempt too, and retrying spends budget the wiki did not offer.
    /// </remarks>
    public bool IsTransient => StatusCode switch
    {
        null => false,
        HttpStatusCode.NotFound => false,
        HttpStatusCode.TooManyRequests => true,
        HttpStatusCode.RequestTimeout => true,
        var code => (int)code >= 500,
    };
}
