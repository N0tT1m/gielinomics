namespace Weatheria.Alerts;

/// <summary>
/// Validates alert webhook destinations against a host allowlist.
/// </summary>
/// <remarks>
/// An alert rule hands the server a URL it will later make outbound requests to.
/// Unconstrained, that is an open request relay pointed at whatever the box can reach.
/// Validate on write, not at fire time; the <c>alert_rules</c> CHECK constraint backs this up.
/// </remarks>
public static class WebhookUrlValidator
{
    /// <summary>Hosts an alert webhook may target.</summary>
    public static string[] AllowedHosts { get; } =
    [
        "discord.com",
        "discordapp.com",
        "canary.discord.com",
        "ptb.discord.com",
    ];

    /// <summary>Whether a URL is an acceptable Discord webhook target.</summary>
    /// <param name="url">The candidate URL.</param>
    /// <returns>True when HTTPS, on an allowed host, and on the webhook path.</returns>
    public static bool IsAllowed(string? url)
        => throw new NotImplementedException(
            "TODO: absolute URI + scheme https + host in AllowedHosts + path starts with /api/webhooks/");
}
