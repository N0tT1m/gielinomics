using Gielinomics.Api.Infrastructure;
using Gielinomics.Client.Hiscores;
using Gielinomics.Data;

namespace Gielinomics.Api.Endpoints;

/// <summary>
/// Tracked account lookup and history.
/// </summary>
/// <remarks>
/// The hiscores endpoint sends no CORS headers, so a browser cannot call it directly. These
/// routes are the concrete reason this API exists — the frontend proxies through them.
/// </remarks>
public static class PlayerEndpoints
{
    /// <summary>Periods the gains route accepts.</summary>
    private static readonly Dictionary<string, TimeSpan> Periods = new(StringComparer.OrdinalIgnoreCase)
    {
        ["day"] = TimeSpan.FromDays(1),
        ["week"] = TimeSpan.FromDays(7),
        ["month"] = TimeSpan.FromDays(30),
        ["year"] = TimeSpan.FromDays(365),
    };

    /// <summary>Maps the <c>/api/players</c> routes.</summary>
    /// <param name="app">The route builder.</param>
    /// <returns>The route builder, for chaining.</returns>
    public static IEndpointRouteBuilder MapPlayerEndpoints(this IEndpointRouteBuilder app)
    {
        ArgumentNullException.ThrowIfNull(app);

        var group = app.MapGroup("/api/players").WithTags("Players");

        group.MapGet("/{name}", GetAsync)
            .WithName("GetPlayer")
            .WithSummary("Resolves an account by any name it has ever used.")
            .Produces<PlayerResponse>()
            .ProducesProblem(StatusCodes.Status404NotFound);

        group.MapGet("/{name}/history", GetHistoryAsync)
            .WithName("GetPlayerHistory")
            .WithSummary("Per-skill history for a tracked account.")
            .Produces<PlayerHistoryResponse>()
            .ProducesProblem(StatusCodes.Status400BadRequest)
            .ProducesProblem(StatusCodes.Status404NotFound);

        group.MapGet("/{name}/gains", GetGainsAsync)
            .WithName("GetPlayerGains")
            .WithSummary("Experience and levels gained over a period.")
            .Produces<PlayerGainsResponse>()
            .ProducesProblem(StatusCodes.Status400BadRequest)
            .ProducesProblem(StatusCodes.Status404NotFound);

        group.MapPost("/{name}/track", TrackAsync)
            .WithName("TrackPlayer")
            .WithSummary("Starts tracking an account. Authenticated: tracking adds polling load.")
            .AddEndpointFilter<ApiTokenEndpointFilter>()
            .Produces<Player>(StatusCodes.Status201Created)
            .ProducesProblem(StatusCodes.Status400BadRequest)
            .ProducesProblem(StatusCodes.Status401Unauthorized)
            .ProducesProblem(StatusCodes.Status404NotFound);

        return app;
    }

    /// <summary>Resolves an account and returns its name history.</summary>
    /// <param name="players">Account storage.</param>
    /// <param name="response">The response, for cache headers.</param>
    /// <param name="name">Any name the account has used.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The account, or 404.</returns>
    private static async Task<IResult> GetAsync(
        PlayerRepository players,
        HttpResponse response,
        string name,
        CancellationToken cancellationToken)
    {
        var player = await players.ResolveAsync(name, cancellationToken).ConfigureAwait(false);
        if (player is null)
        {
            return NotTracked(name);
        }

        var names = await players.GetNamesAsync(player.Id, cancellationToken).ConfigureAwait(false);

        QueryConventions.CacheFor(response, TimeSpan.FromMinutes(5));
        return Results.Ok(new PlayerResponse(player, names));
    }

    /// <summary>Per-skill history.</summary>
    /// <param name="players">Account storage.</param>
    /// <param name="response">The response, for cache headers.</param>
    /// <param name="name">Any name the account has used.</param>
    /// <param name="skill">A single skill index, or null for all.</param>
    /// <param name="from">Inclusive lower bound. Defaults to 30 days ago.</param>
    /// <param name="limit">Maximum rows.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The samples.</returns>
    private static async Task<IResult> GetHistoryAsync(
        PlayerRepository players,
        HttpResponse response,
        string name,
        short? skill,
        DateTimeOffset? from,
        int? limit,
        CancellationToken cancellationToken)
    {
        var player = await players.ResolveAsync(name, cancellationToken).ConfigureAwait(false);
        if (player is null)
        {
            return NotTracked(name);
        }

        if (skill is { } index && (index < 0 || index >= HiscoreMapping.Current.SkillNames.Count))
        {
            return Results.Problem(
                title: "Unknown skill",
                detail: $"skill must be between 0 and {HiscoreMapping.Current.SkillNames.Count - 1}.",
                statusCode: StatusCodes.Status400BadRequest);
        }

        var samples = await players
            .GetHistoryAsync(
                player.Id,
                skill,
                from ?? DateTimeOffset.UtcNow.AddDays(-30),
                Math.Clamp(limit ?? QueryConventions.MaxPricePoints, 1, QueryConventions.MaxPricePoints),
                cancellationToken)
            .ConfigureAwait(false);

        QueryConventions.CacheFor(response, TimeSpan.FromMinutes(5));

        // The mapping is echoed back so a client never has to guess what skill 21 is.
        return Results.Ok(new PlayerHistoryResponse(
            player.DisplayName,
            HiscoreMapping.Current.Version,
            HiscoreMapping.Current.SkillNames,
            samples));
    }

    /// <summary>Gains over a period.</summary>
    /// <param name="players">Account storage.</param>
    /// <param name="response">The response, for cache headers.</param>
    /// <param name="name">Any name the account has used.</param>
    /// <param name="period">One of day, week, month, year.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The gains, largest first.</returns>
    private static async Task<IResult> GetGainsAsync(
        PlayerRepository players,
        HttpResponse response,
        string name,
        string? period,
        CancellationToken cancellationToken)
    {
        var player = await players.ResolveAsync(name, cancellationToken).ConfigureAwait(false);
        if (player is null)
        {
            return NotTracked(name);
        }

        var window = TimeSpan.FromDays(7);
        if (!string.IsNullOrWhiteSpace(period) && !Periods.TryGetValue(period, out window))
        {
            return Results.Problem(
                title: "Unknown period",
                detail: $"period must be one of: {string.Join(", ", Periods.Keys)}.",
                statusCode: StatusCodes.Status400BadRequest);
        }

        var gains = await players
            .GetGainsAsync(player.Id, DateTimeOffset.UtcNow - window, HiscoreMapping.Current.SkillNames, cancellationToken)
            .ConfigureAwait(false);

        // Overall (index 0) is the sum of the rest, so it would always top a ranking by gain
        // and tell nobody anything. It is reported separately instead.
        var overall = gains.FirstOrDefault(gain => gain.Skill == 0);
        var bySkill = gains.Where(gain => gain.Skill != 0).OrderByDescending(gain => gain.GainedXp).ToList();

        QueryConventions.CacheFor(response, TimeSpan.FromMinutes(5));

        return Results.Ok(new PlayerGainsResponse(player.DisplayName, window, overall, bySkill));
    }

    /// <summary>
    /// Starts tracking an account.
    /// </summary>
    /// <remarks>
    /// Authenticated, because tracking is what creates polling load against an upstream that
    /// publishes no rate limit. Account type is detected once here rather than on every poll:
    /// detection costs up to one request per hiscore table.
    /// </remarks>
    /// <param name="players">Account storage.</param>
    /// <param name="hiscores">Used to verify the account exists and infer its type.</param>
    /// <param name="http">The authenticated request.</param>
    /// <param name="name">The display name to track.</param>
    /// <param name="cancellationToken">Cancels the write.</param>
    /// <returns>The tracked account.</returns>
    private static async Task<IResult> TrackAsync(
        PlayerRepository players,
        IHiscoresClient hiscores,
        HttpContext http,
        string name,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(name) || name.Length > 12)
        {
            // Names are at most 12 characters. Rejecting longer ones here keeps a junk request
            // from costing ten upstream lookups.
            return Results.Problem(
                title: "Invalid name",
                detail: "name must be between 1 and 12 characters.",
                statusCode: StatusCodes.Status400BadRequest);
        }

        var table = await hiscores.DetectAccountTypeAsync(name, cancellationToken).ConfigureAwait(false);
        if (table is null)
        {
            return Results.Problem(
                title: "Player not found",
                detail: $"'{name}' does not appear on any hiscore table. Unranked accounts are not listed.",
                statusCode: StatusCodes.Status404NotFound);
        }

        var player = await players.TrackAsync(name, table.Value.ToString(), cancellationToken).ConfigureAwait(false);

        http.Response.Headers.CacheControl = "no-store";
        return Results.Created($"/api/players/{Uri.EscapeDataString(player.DisplayName)}", player);
    }

    /// <summary>The 404 used for an account nobody has asked to track.</summary>
    /// <param name="name">The name that was looked up.</param>
    /// <returns>The problem result.</returns>
    private static IResult NotTracked(string name)
        => Results.Problem(
            title: "Player not tracked",
            detail: $"'{name}' is not tracked. POST /api/players/{{name}}/track to start collecting history for it.",
            statusCode: StatusCodes.Status404NotFound);
}
