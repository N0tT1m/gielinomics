using System.Net.Http.Json;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Logging;

namespace Gielinomics.Alerts;

/// <summary>A notification ready to send.</summary>
/// <param name="Title">Headline.</param>
/// <param name="Description">Body text.</param>
/// <param name="Url">Optional link back into the platform.</param>
public sealed record AlertNotification(string Title, string Description, string? Url = null);

/// <summary>Sends a fired alert somewhere a person will see it.</summary>
public interface IAlertDispatcher
{
    /// <summary>Delivers one notification.</summary>
    /// <param name="webhookUrl">Destination, already validated against the host allowlist.</param>
    /// <param name="notification">What to send.</param>
    /// <param name="cancellationToken">Cancels the send.</param>
    /// <returns>True when the destination accepted it.</returns>
    Task<bool> DispatchAsync(string webhookUrl, AlertNotification notification, CancellationToken cancellationToken = default);
}

/// <summary>
/// Posts notifications to a Discord webhook.
/// </summary>
/// <remarks>
/// Re-validates the destination immediately before the request. The rule was validated on
/// write and the table has a CHECK constraint, but this is the last line before an outbound
/// request leaves the box, and it is the cheapest place to be certain.
/// </remarks>
/// <param name="httpClient">The HTTP client to send with.</param>
/// <param name="logger">Log sink.</param>
public sealed class DiscordWebhookDispatcher(HttpClient httpClient, ILogger<DiscordWebhookDispatcher> logger) : IAlertDispatcher
{
    private readonly HttpClient _httpClient = httpClient;
    private readonly ILogger<DiscordWebhookDispatcher> _logger = logger;

    /// <inheritdoc />
    public async Task<bool> DispatchAsync(
        string webhookUrl,
        AlertNotification notification,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(notification);

        if (!WebhookUrlValidator.IsAllowed(webhookUrl))
        {
            _logger.LogError("Refusing to dispatch to a webhook URL that is not on the allowlist.");
            return false;
        }

        var payload = new DiscordWebhookPayload
        {
            Embeds =
            [
                new DiscordEmbed
                {
                    Title = notification.Title,
                    Description = notification.Description,
                    Url = notification.Url,
                    Color = EmbedColor,
                },
            ],
        };

        try
        {
            using var response = await _httpClient
                .PostAsJsonAsync(webhookUrl, payload, cancellationToken)
                .ConfigureAwait(false);

            if (response.IsSuccessStatusCode)
            {
                return true;
            }

            _logger.LogWarning(
                "Discord rejected an alert with {StatusCode} {Reason}.",
                (int)response.StatusCode,
                response.ReasonPhrase);

            return false;
        }
        catch (HttpRequestException ex)
        {
            // Swallowed deliberately: one unreachable webhook must not abort the sweep and
            // starve every other rule of its evaluation.
            _logger.LogWarning(ex, "Failed to reach a Discord webhook.");
            return false;
        }
        catch (TaskCanceledException ex) when (!cancellationToken.IsCancellationRequested)
        {
            _logger.LogWarning(ex, "Timed out posting to a Discord webhook.");
            return false;
        }
    }

    /// <summary>Embed accent colour, as Discord's packed 24-bit integer.</summary>
    private const int EmbedColor = 0xB0_7A2A;

    /// <summary>The webhook request body.</summary>
    private sealed record DiscordWebhookPayload
    {
        /// <summary>The embeds to render.</summary>
        [JsonPropertyName("embeds")]
        public required IReadOnlyList<DiscordEmbed> Embeds { get; init; }
    }

    /// <summary>One rendered embed.</summary>
    private sealed record DiscordEmbed
    {
        /// <summary>Headline.</summary>
        [JsonPropertyName("title")]
        public required string Title { get; init; }

        /// <summary>Body text.</summary>
        [JsonPropertyName("description")]
        public required string Description { get; init; }

        /// <summary>Optional link.</summary>
        [JsonPropertyName("url")]
        public string? Url { get; init; }

        /// <summary>Accent colour.</summary>
        [JsonPropertyName("color")]
        public int Color { get; init; }
    }
}
