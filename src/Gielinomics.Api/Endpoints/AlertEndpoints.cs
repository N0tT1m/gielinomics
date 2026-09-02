using System.Text.Json;
using Gielinomics.Api.Infrastructure;
using Gielinomics.Alerts;
using Gielinomics.Data;

namespace Gielinomics.Api.Endpoints;

/// <summary>Request body for creating an alert rule.</summary>
/// <param name="Kind">Rule kind — see <see cref="AlertRuleKind"/>.</param>
/// <param name="Config">Rule configuration, shaped by the kind.</param>
/// <param name="WebhookUrl">Discord webhook to notify.</param>
public sealed record CreateAlertRequest(string? Kind, JsonElement Config, string? WebhookUrl);

/// <summary>
/// Alert rule management.
/// </summary>
/// <remarks>
/// Both routes are authenticated. The POST is the more dangerous of the two: it accepts a URL
/// the server will later make outbound requests to, which unconstrained is an open request
/// relay pointed at the internal network.
/// </remarks>
public static class AlertEndpoints
{
    /// <summary>Maps the <c>/api/alerts</c> routes.</summary>
    /// <param name="app">The route builder.</param>
    /// <returns>The route builder, for chaining.</returns>
    public static IEndpointRouteBuilder MapAlertEndpoints(this IEndpointRouteBuilder app)
    {
        ArgumentNullException.ThrowIfNull(app);

        var group = app.MapGroup("/api/alerts")
            .WithTags("Alerts")
            .AddEndpointFilter<ApiTokenEndpointFilter>();

        group.MapGet("/", ListAsync)
            .WithName("ListAlerts")
            .WithSummary("Lists the calling token's alert rules.");

        group.MapPost("/", CreateAsync)
            .WithName("CreateAlert")
            .WithSummary("Creates an alert rule. The webhook host is validated on write.");

        return app;
    }

    /// <summary>Lists the caller's rules.</summary>
    /// <param name="alerts">Rule storage.</param>
    /// <param name="http">The authenticated request.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The caller's rules.</returns>
    private static async Task<IResult> ListAsync(
        AlertRepository alerts,
        HttpContext http,
        CancellationToken cancellationToken)
    {
        var user = ApiTokenEndpointFilter.GetUser(http);
        var rules = await alerts.ListAsync(user.Id, cancellationToken).ConfigureAwait(false);

        // Never cached. These are per-token and a shared cache keyed on the URL alone would
        // hand one caller another's rules.
        http.Response.Headers.CacheControl = "no-store";

        return Results.Ok(new { rules });
    }

    /// <summary>Creates a rule.</summary>
    /// <param name="alerts">Rule storage.</param>
    /// <param name="http">The authenticated request.</param>
    /// <param name="request">The rule to create.</param>
    /// <param name="cancellationToken">Cancels the write.</param>
    /// <returns>The created rule.</returns>
    private static async Task<IResult> CreateAsync(
        AlertRepository alerts,
        HttpContext http,
        CreateAlertRequest request,
        CancellationToken cancellationToken)
    {
        if (request is null)
        {
            return Results.Problem(
                title: "Missing body",
                detail: "A rule body is required.",
                statusCode: StatusCodes.Status400BadRequest);
        }

        // The host allowlist, checked before anything is stored. The alert_rules CHECK
        // constraint backs this up, but a clear 400 beats a constraint violation.
        if (!WebhookUrlValidator.IsAllowed(request.WebhookUrl))
        {
            return Results.Problem(
                title: "Webhook not allowed",
                detail: $"webhookUrl must be an https Discord webhook on one of: {string.Join(", ", WebhookUrlValidator.AllowedHosts)}.",
                statusCode: StatusCodes.Status400BadRequest);
        }

        var kind = request.Kind ?? string.Empty;
        var configJson = request.Config.ValueKind == JsonValueKind.Undefined
            ? "{}"
            : request.Config.GetRawText();

        if (!AlertRuleConfig.TryValidate(kind, configJson, out var error))
        {
            return Results.Problem(
                title: "Invalid rule",
                detail: error,
                statusCode: StatusCodes.Status400BadRequest);
        }

        var user = ApiTokenEndpointFilter.GetUser(http);
        var rule = await alerts
            .CreateAsync(user.Id, kind, configJson, request.WebhookUrl!, cancellationToken)
            .ConfigureAwait(false);

        http.Response.Headers.CacheControl = "no-store";

        return Results.Created($"/api/alerts/{rule.Id}", rule);
    }
}
