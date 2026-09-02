using System.Threading.RateLimiting;

namespace Gielinomics.Ingest.Infrastructure;

/// <summary>
/// Paces outbound requests to one upstream host.
/// </summary>
/// <remarks>
/// <para>
/// One limiter per host, because the hosts do not share a budget or a temperament: the wiki
/// asks politely for reasonable use, Jagex is considerably less forgiving. Sharing a single
/// global limiter would let a backfill against one starve the live poll against the other.
/// </para>
/// <para>
/// This queues rather than rejecting. A rejected request would surface as a failure the
/// retry policy then retries, turning self-imposed pacing into upstream pressure — the exact
/// opposite of the point.
/// </para>
/// </remarks>
/// <param name="limiter">
/// The limiter governing this host. Owned by the container and shared across every handler
/// instance — the handler deliberately does not dispose it, since a per-instance limiter
/// would impose no shared budget at all.
/// </param>
public sealed class RateLimitingHandler(RateLimiter limiter) : DelegatingHandler
{
    private readonly RateLimiter _limiter = limiter;

    /// <inheritdoc />
    protected override async Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request,
        CancellationToken cancellationToken)
    {
        using var lease = await _limiter.AcquireAsync(permitCount: 1, cancellationToken).ConfigureAwait(false);

        if (!lease.IsAcquired)
        {
            // The queue is full, which means the caller is generating work faster than the
            // budget allows. Say so plainly rather than sending the request anyway.
            throw new HttpRequestException(
                $"Rate limit queue for {request.RequestUri?.Host} is full; request was not sent.");
        }

        return await base.SendAsync(request, cancellationToken).ConfigureAwait(false);
    }
}
