using Gielinomics.Api.Infrastructure;
using Gielinomics.Data;

namespace Gielinomics.Api.Endpoints;

/// <summary>
/// Visibility into the ingest pipeline.
/// </summary>
/// <remarks>
/// Public on purpose. The dataset is the product, and a consumer cannot judge an answer
/// without knowing how complete the history behind it is.
/// </remarks>
public static class IngestEndpoints
{
    /// <summary>Maps the <c>/api/ingest</c> routes.</summary>
    /// <param name="app">The route builder.</param>
    /// <returns>The route builder, for chaining.</returns>
    public static IEndpointRouteBuilder MapIngestEndpoints(this IEndpointRouteBuilder app)
    {
        ArgumentNullException.ThrowIfNull(app);

        var group = app.MapGroup("/api/ingest").WithTags("Ingest");

        group.MapGet("/status", GetStatusAsync)
            .WithName("GetIngestStatus")
            .WithSummary("Per-feed last success and recent failure counts.")
            .Produces<IngestStatusResponse>();

        group.MapGet("/coverage", GetCoverageAsync)
            .WithName("GetIngestCoverage")
            .WithSummary("Fraction of the expected windows actually retained.")
            .Produces<CoverageReport>()
            .ProducesProblem(StatusCodes.Status400BadRequest);

        return app;
    }

    /// <summary>Per-feed health.</summary>
    /// <param name="ingest">Audit trail reads.</param>
    /// <param name="response">The response, for cache headers.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>Feed statuses.</returns>
    private static async Task<IResult> GetStatusAsync(
        IngestQueryRepository ingest,
        HttpResponse response,
        CancellationToken cancellationToken)
    {
        var feeds = await ingest.GetStatusAsync(cancellationToken).ConfigureAwait(false);

        QueryConventions.CacheFor(response, TimeSpan.FromSeconds(30));
        return Results.Ok(new IngestStatusResponse(feeds));
    }

    /// <summary>Coverage of a window at a granularity.</summary>
    /// <param name="ingest">Audit trail reads.</param>
    /// <param name="response">The response, for cache headers.</param>
    /// <param name="interval">Granularity to inspect.</param>
    /// <param name="window">How far back to inspect.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The coverage report.</returns>
    private static async Task<IResult> GetCoverageAsync(
        IngestQueryRepository ingest,
        HttpResponse response,
        string? interval,
        string? window,
        CancellationToken cancellationToken)
    {
        if (!QueryConventions.TryResolveInterval(interval, out var stepSeconds))
        {
            return Results.Problem(
                title: "Unsupported interval",
                detail: $"interval must be one of: {string.Join(", ", QueryConventions.Intervals.Keys)}.",
                statusCode: StatusCodes.Status400BadRequest);
        }

        if (!QueryConventions.TryParseWindow(window, TimeSpan.FromDays(1), out var lookback))
        {
            return Results.Problem(
                title: "Invalid window",
                detail: "window must be a positive magnitude followed by m, h, d or w — for example 24h or 7d.",
                statusCode: StatusCodes.Status400BadRequest);
        }

        // Measured to the last closed window, not to now: the window in progress has no rows
        // yet by definition, and counting it would report a permanent shortfall.
        var to = DateTimeOffset.FromUnixTimeSeconds(
            DateTimeOffset.UtcNow.ToUnixTimeSeconds() / stepSeconds * stepSeconds).AddSeconds(-stepSeconds);

        var coverage = await ingest
            .GetCoverageAsync(stepSeconds, to - lookback, to, cancellationToken)
            .ConfigureAwait(false);

        QueryConventions.CacheFor(response, TimeSpan.FromMinutes(1));
        return Results.Ok(coverage);
    }
}
