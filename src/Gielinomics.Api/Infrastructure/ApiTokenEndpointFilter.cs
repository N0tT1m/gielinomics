using System.Security.Cryptography;
using System.Text;
using Gielinomics.Data;

namespace Gielinomics.Api.Infrastructure;

/// <summary>
/// Requires a valid API token on the routes it is attached to.
/// </summary>
/// <remarks>
/// <para>
/// The two write routes are the only mutable surface and both are abusable: tracking adds
/// unbounded polling load, and an alert rule hands the server a URL it will later make
/// outbound requests to. Neither may be reachable without a token.
/// </para>
/// <para>
/// Tokens are compared by SHA-256 hash. The database stores the hash, so a dump of
/// <c>api_users</c> does not hand anyone a working credential.
/// </para>
/// </remarks>
/// <param name="users">Token lookup.</param>
public sealed class ApiTokenEndpointFilter(ApiUserRepository users) : IEndpointFilter
{
    /// <summary>Key under which the authenticated caller is stashed on the request.</summary>
    public const string ApiUserItemKey = "gielinomics.api_user";

    private const string BearerPrefix = "Bearer ";

    private readonly ApiUserRepository _users = users;

    /// <inheritdoc />
    public async ValueTask<object?> InvokeAsync(EndpointFilterInvocationContext context, EndpointFilterDelegate next)
    {
        ArgumentNullException.ThrowIfNull(context);
        ArgumentNullException.ThrowIfNull(next);

        var http = context.HttpContext;

        if (!TryReadToken(http.Request, out var token))
        {
            return Challenge(http);
        }

        var hash = SHA256.HashData(Encoding.UTF8.GetBytes(token));
        var user = await _users.FindByTokenHashAsync(hash, http.RequestAborted).ConfigureAwait(false);

        if (user is null)
        {
            return Challenge(http);
        }

        http.Items[ApiUserItemKey] = user;
        return await next(context).ConfigureAwait(false);
    }

    /// <summary>The caller a filtered request authenticated as.</summary>
    /// <param name="http">The request.</param>
    /// <returns>The caller.</returns>
    /// <exception cref="InvalidOperationException">The route was not behind this filter.</exception>
    public static ApiUser GetUser(HttpContext http)
    {
        ArgumentNullException.ThrowIfNull(http);

        return http.Items[ApiUserItemKey] as ApiUser
            ?? throw new InvalidOperationException(
                "No authenticated caller on the request. This route is missing the API token filter.");
    }

    /// <summary>Extracts a bearer token from the request.</summary>
    /// <param name="request">The request.</param>
    /// <param name="token">The token, when present.</param>
    /// <returns>True when a non-empty bearer token was supplied.</returns>
    private static bool TryReadToken(HttpRequest request, out string token)
    {
        token = string.Empty;

        var header = request.Headers.Authorization.ToString();
        if (!header.StartsWith(BearerPrefix, StringComparison.OrdinalIgnoreCase))
        {
            return false;
        }

        token = header[BearerPrefix.Length..].Trim();
        return token.Length > 0;
    }

    /// <summary>
    /// Returns 401 without saying which half of the credential was wrong.
    /// </summary>
    /// <remarks>
    /// A missing token and an unrecognised one produce the identical response. Distinguishing
    /// them turns the endpoint into an oracle for whether a guessed token exists.
    /// </remarks>
    /// <param name="http">The request.</param>
    /// <returns>The 401 result.</returns>
    private static IResult Challenge(HttpContext http)
    {
        http.Response.Headers.WWWAuthenticate = "Bearer";
        return Results.Problem(
            title: "Unauthorized",
            detail: "A valid API token is required. Send it as 'Authorization: Bearer <token>'.",
            statusCode: StatusCodes.Status401Unauthorized);
    }
}
