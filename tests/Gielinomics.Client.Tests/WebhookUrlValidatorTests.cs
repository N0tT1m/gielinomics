using Gielinomics.Alerts;
using Xunit;

namespace Gielinomics.Client.Tests;

/// <summary>
/// An alert rule hands the server a URL it will later make outbound requests to.
/// Every case here is a way that becomes a request relay pointed at the internal network.
/// </summary>
public class WebhookUrlValidatorTests
{
    [Theory]
    [InlineData("https://discord.com/api/webhooks/123/abc")]
    [InlineData("https://discordapp.com/api/webhooks/123/abc")]
    [InlineData("https://canary.discord.com/api/webhooks/123/abc")]
    [InlineData("https://ptb.discord.com/api/webhooks/123/abc")]
    [InlineData("https://DISCORD.COM/api/webhooks/123/abc")]
    public void Accepts_https_discord_webhooks(string url)
        => Assert.True(WebhookUrlValidator.IsAllowed(url));

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("not a url")]
    [InlineData("/api/webhooks/123/abc")]                          // Relative: no host to check.
    [InlineData("http://discord.com/api/webhooks/123/abc")]        // Plaintext.
    [InlineData("ftp://discord.com/api/webhooks/123/abc")]
    [InlineData("file:///etc/passwd")]
    [InlineData("https://discord.com.evil.example/api/webhooks/1")] // Suffix, not the host.
    [InlineData("https://evil.example/api/webhooks/123/abc")]
    [InlineData("https://discord.com/api/webhooks/")]               // No webhook identity.
    [InlineData("https://discord.com/api/other/123")]
    [InlineData("https://discord.com/")]
    [InlineData("https://169.254.169.254/api/webhooks/1/2")]        // Cloud metadata endpoint.
    [InlineData("https://localhost/api/webhooks/1/2")]
    public void Rejects_everything_else(string? url)
        => Assert.False(WebhookUrlValidator.IsAllowed(url));

    [Fact]
    public void Rejects_credentials_in_the_authority()
    {
        // Reads as an allowed host to anything doing a substring check. The host here is
        // internal.example, not discord.com.
        Assert.False(WebhookUrlValidator.IsAllowed("https://discord.com@internal.example/api/webhooks/1/2"));
    }

    [Fact]
    public void Rejects_a_non_default_port_on_an_allowed_host()
    {
        Assert.False(WebhookUrlValidator.IsAllowed("https://discord.com:8443/api/webhooks/1/2"));
    }

    [Fact]
    public void Rejects_an_allowed_url_embedded_in_a_foreign_path()
    {
        // A plain "does it contain discord.com/api/webhooks" check accepts this. The host
        // is evil.example; the rest is just path.
        Assert.False(WebhookUrlValidator.IsAllowed("https://evil.example/https://discord.com/api/webhooks/1/2"));
    }

    [Theory]
    [InlineData("https://discord.com/api/../api/webhooks/1/2")]
    [InlineData("https://discord.com/api/%77ebhooks/1/2")]
    public void Accepts_paths_that_normalise_onto_the_webhook_prefix(string url)
    {
        // Uri resolves dot segments and decodes percent-encoded unreserved characters before
        // the check sees them, so both of these genuinely are /api/webhooks/1/2 on discord.com.
        // Matching on AbsolutePath is what makes them resolve rather than sneak past.
        Assert.True(WebhookUrlValidator.IsAllowed(url));
    }
}
