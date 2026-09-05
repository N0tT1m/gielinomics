using System.Net;
using Gielinomics.Alerts;
using Microsoft.Extensions.Logging;
using Xunit;

namespace Gielinomics.Alerts.Tests;

/// <summary>
/// The dispatcher is the last code that runs before an outbound request leaves the box.
/// </summary>
/// <remarks>
/// Two properties matter: a destination off the allowlist never becomes a request, and a
/// failure of any kind returns false instead of throwing, because an exception here aborts
/// the sweep and starves every other rule.
/// </remarks>
public sealed class DiscordWebhookDispatcherTests
{
    private const string ValidWebhook = "https://discord.com/api/webhooks/123/abc";

    private static readonly AlertNotification Notification = new("Abyssal whip", "100,000 gp margin");

    [Fact]
    public async Task Posts_to_an_allowlisted_webhook()
    {
        var (dispatcher, handler, _) = Build();

        var sent = await dispatcher.DispatchAsync(ValidWebhook, Notification);

        Assert.True(sent);
        var request = Assert.Single(handler.Requests);
        Assert.Equal(new Uri(ValidWebhook), request.Uri);
    }

    [Fact]
    public async Task Sends_the_notification_as_a_single_embed()
    {
        var (dispatcher, handler, _) = Build();

        await dispatcher.DispatchAsync(
            ValidWebhook,
            new AlertNotification("Title here", "Body here", "https://example.invalid/item/4151"));

        var body = Assert.Single(handler.Requests).Body;
        Assert.Contains("\"embeds\"", body, StringComparison.Ordinal);
        Assert.Contains("Title here", body, StringComparison.Ordinal);
        Assert.Contains("Body here", body, StringComparison.Ordinal);
        Assert.Contains("https://example.invalid/item/4151", body, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("http://discord.com/api/webhooks/123/abc")]
    [InlineData("https://evil.invalid/api/webhooks/123/abc")]
    [InlineData("https://discord.com@evil.invalid/api/webhooks/123/abc")]
    [InlineData("https://discord.com:8443/api/webhooks/123/abc")]
    [InlineData("https://discord.com/")]
    [InlineData("http://169.254.169.254/latest/meta-data/")]
    [InlineData("")]
    public async Task Refuses_a_destination_off_the_allowlist_without_sending_anything(string url)
    {
        var (dispatcher, handler, logger) = Build();

        var sent = await dispatcher.DispatchAsync(url, Notification);

        Assert.False(sent);
        Assert.Empty(handler.Requests);
        Assert.Contains(logger.Entries, e => e.Level == LogLevel.Error);
    }

    [Fact]
    public async Task Reports_failure_when_discord_rejects_the_post()
    {
        var (dispatcher, _, logger) = Build(new RecordingHandler(HttpStatusCode.TooManyRequests));

        var sent = await dispatcher.DispatchAsync(ValidWebhook, Notification);

        Assert.False(sent);
        Assert.Contains(logger.Entries, e => e.Level == LogLevel.Warning);
    }

    [Fact]
    public async Task Swallows_a_transport_failure_so_one_dead_webhook_cannot_abort_the_sweep()
    {
        var handler = new RecordingHandler(_ => throw new HttpRequestException("connection refused"));
        var (dispatcher, _, logger) = Build(handler);

        var sent = await dispatcher.DispatchAsync(ValidWebhook, Notification);

        Assert.False(sent);
        Assert.Contains(logger.Entries, e => e.Level == LogLevel.Warning);
    }

    [Fact]
    public async Task Swallows_a_timeout()
    {
        var handler = new RecordingHandler(_ => throw new TaskCanceledException("timed out"));
        var (dispatcher, _, logger) = Build(handler);

        var sent = await dispatcher.DispatchAsync(ValidWebhook, Notification);

        Assert.False(sent);
        Assert.Contains(logger.Entries, e => e.Level == LogLevel.Warning);
    }

    [Fact]
    public async Task Propagates_a_caller_requested_cancellation()
    {
        // Distinct from a timeout: a cancelled sweep should stop, not log and carry on.
        var handler = new RecordingHandler(_ => throw new TaskCanceledException("cancelled"));
        var (dispatcher, _, _) = Build(handler);
        using var cts = new CancellationTokenSource();
        await cts.CancelAsync();

        await Assert.ThrowsAnyAsync<OperationCanceledException>(
            () => dispatcher.DispatchAsync(ValidWebhook, Notification, cts.Token));
    }

    [Fact]
    public async Task Rejects_a_null_notification()
    {
        var (dispatcher, _, _) = Build();

        await Assert.ThrowsAsync<ArgumentNullException>(
            () => dispatcher.DispatchAsync(ValidWebhook, null!));
    }

    /// <summary>Builds a dispatcher over a recording handler.</summary>
    /// <param name="handler">The handler to use; a 204-replying one by default.</param>
    /// <returns>The dispatcher, its handler and its log sink.</returns>
    private static (DiscordWebhookDispatcher Dispatcher, RecordingHandler Handler, CapturingLogger<DiscordWebhookDispatcher> Logger)
        Build(RecordingHandler? handler = null)
    {
        handler ??= new RecordingHandler();
        var logger = new CapturingLogger<DiscordWebhookDispatcher>();
        return (new DiscordWebhookDispatcher(new HttpClient(handler), logger), handler, logger);
    }
}
