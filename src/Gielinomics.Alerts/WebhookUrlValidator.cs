namespace Gielinomics.Alerts;

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

    /// <summary>The only path prefix a webhook may use.</summary>
    private const string WebhookPathPrefix = "/api/webhooks/";

    /// <summary>Whether a URL is an acceptable Discord webhook target.</summary>
    /// <param name="url">The candidate URL.</param>
    /// <returns>True when HTTPS, on an allowed host, and on the webhook path.</returns>
    public static bool IsAllowed(string? url)
    {
        if (string.IsNullOrWhiteSpace(url))
        {
            return false;
        }

        // Absolute only. A relative URI has no host to check, and Uri would happily
        // resolve one against a base later, which is exactly the bypass this guards.
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri))
        {
            return false;
        }

        if (!string.Equals(uri.Scheme, Uri.UriSchemeHttps, StringComparison.Ordinal))
        {
            return false;
        }

        // Credentials in the authority are how "https://discord.com@internal.host/..."
        // reads as an allowed host to a careless parser. Uri gets this right, but a
        // userinfo section has no legitimate place in a webhook URL either way.
        if (!string.IsNullOrEmpty(uri.UserInfo))
        {
            return false;
        }

        // Default port only. An allowed hostname on an arbitrary port is still an
        // arbitrary destination if that hostname resolves somewhere unexpected.
        if (!uri.IsDefaultPort)
        {
            return false;
        }

        if (!AllowedHosts.Contains(uri.Host, StringComparer.OrdinalIgnoreCase))
        {
            return false;
        }

        // AbsolutePath, not the original string: the parsed path has already had
        // percent-encoding and dot segments resolved, so "/api/../api/webhooks/" and
        // "/api/%77ebhooks/" cannot smuggle past a raw prefix comparison.
        return uri.AbsolutePath.StartsWith(WebhookPathPrefix, StringComparison.Ordinal)
            && uri.AbsolutePath.Length > WebhookPathPrefix.Length;
    }
}
