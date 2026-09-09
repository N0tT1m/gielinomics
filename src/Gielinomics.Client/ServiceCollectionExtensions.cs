using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using Gielinomics.Client.Hiscores;
using Gielinomics.Client.Prices;
using Gielinomics.Client.Wiki;
using Gielinomics.Client.WiseOldMan;

namespace Gielinomics.Client;

/// <summary>DI registration for the Gielinomics clients.</summary>
public static class ServiceCollectionExtensions
{
    /// <summary>Named <see cref="HttpClient"/> for the prices API. Use this to attach your own resilience policy.</summary>
    public const string PricesHttpClientName = "gielinomics.prices";

    /// <summary>Named <see cref="HttpClient"/> for the wiki's Bucket API.</summary>
    public const string WikiHttpClientName = "gielinomics.wiki";

    /// <summary>Named <see cref="HttpClient"/> for the official hiscores.</summary>
    /// <remarks>
    /// Separate from the prices client so the two get separate resilience and rate limit
    /// budgets. Jagex is considerably less forgiving than the wiki, and one shared budget
    /// would let a hiscore sweep spend the price poll's allowance.
    /// </remarks>
    public const string HiscoresHttpClientName = "gielinomics.hiscores";

    /// <summary>Named <see cref="HttpClient"/> for Wise Old Man.</summary>
    /// <remarks>
    /// Its own budget again, and the tightest of the three: 20 requests per 60 seconds without
    /// an API key. Sharing a limiter with the wiki would let a price backfill consume an
    /// allowance a hundredth its size before a single group lookup got through.
    /// </remarks>
    public const string WiseOldManHttpClientName = "gielinomics.wiseoldman";

    /// <summary>
    /// Registers <see cref="IPricesClient"/> and its <see cref="HttpClient"/>.
    /// </summary>
    /// <param name="services">The service collection.</param>
    /// <param name="configure">Sets options. <see cref="GielinomicsClientOptions.UserAgent"/> is required.</param>
    /// <returns>The <see cref="IHttpClientBuilder"/> for the prices client, so callers can add resilience.</returns>
    /// <exception cref="ArgumentNullException">Any argument is null.</exception>
    public static IHttpClientBuilder AddGielinomicsClient(
        this IServiceCollection services,
        Action<GielinomicsClientOptions> configure)
    {
        ArgumentNullException.ThrowIfNull(services);
        ArgumentNullException.ThrowIfNull(configure);

        services.AddOptions<GielinomicsClientOptions>()
            .Configure(configure)
            .ValidateDataAnnotations()
            .ValidateOnStart();

        return services.AddHttpClient<IPricesClient, PricesClient>(PricesHttpClientName, (provider, http) =>
        {
            var options = provider.GetRequiredService<IOptions<GielinomicsClientOptions>>().Value;

            http.BaseAddress = options.PricesBaseAddress;
            http.Timeout = options.Timeout;
            http.DefaultRequestHeaders.UserAgent.ParseAdd(options.UserAgent);
        });
    }

    /// <summary>
    /// Registers <see cref="IHiscoresClient"/> and its <see cref="HttpClient"/>.
    /// </summary>
    /// <remarks>
    /// Assumes <see cref="AddGielinomicsClient"/> has already configured the options.
    /// </remarks>
    /// <param name="services">The service collection.</param>
    /// <returns>The <see cref="IHttpClientBuilder"/> for the hiscores client.</returns>
    /// <exception cref="ArgumentNullException"><paramref name="services"/> is null.</exception>
    public static IHttpClientBuilder AddGielinomicsHiscoresClient(this IServiceCollection services)
    {
        ArgumentNullException.ThrowIfNull(services);

        return services.AddHttpClient<IHiscoresClient, HiscoresClient>(HiscoresHttpClientName, (provider, http) =>
        {
            var options = provider.GetRequiredService<IOptions<GielinomicsClientOptions>>().Value;

            http.BaseAddress = options.HiscoresBaseAddress;
            http.Timeout = options.Timeout;
            http.DefaultRequestHeaders.UserAgent.ParseAdd(options.UserAgent);
        });
    }

    /// <summary>
    /// Registers <see cref="IWikiBucketClient"/> and its <see cref="HttpClient"/>.
    /// </summary>
    /// <remarks>
    /// Assumes <see cref="AddGielinomicsClient"/> has already configured the options.
    /// </remarks>
    /// <param name="services">The service collection.</param>
    /// <returns>The <see cref="IHttpClientBuilder"/> for the wiki client.</returns>
    /// <exception cref="ArgumentNullException"><paramref name="services"/> is null.</exception>
    public static IHttpClientBuilder AddGielinomicsWikiClient(this IServiceCollection services)
    {
        ArgumentNullException.ThrowIfNull(services);

        return services.AddHttpClient<IWikiBucketClient, WikiBucketClient>(WikiHttpClientName, (provider, http) =>
        {
            var options = provider.GetRequiredService<IOptions<GielinomicsClientOptions>>().Value;

            http.BaseAddress = options.WikiBaseAddress;

            // A bucket page is 5000 rows of JSON; the default per-request timeout is tight for
            // that, and this runs weekly rather than on a poll cadence.
            http.Timeout = options.Timeout > TimeSpan.FromSeconds(60) ? options.Timeout : TimeSpan.FromSeconds(60);
            http.DefaultRequestHeaders.UserAgent.ParseAdd(options.UserAgent);
        });
    }

    /// <summary>
    /// Registers <see cref="IWiseOldManClient"/> and its <see cref="HttpClient"/>.
    /// </summary>
    /// <remarks>
    /// Assumes <see cref="AddGielinomicsClient"/> has already configured the options. The API
    /// key, when <see cref="GielinomicsClientOptions.WiseOldManApiKey"/> is set, is attached
    /// here as <c>x-api-key</c> — it raises the rate limit and is not otherwise required.
    /// </remarks>
    /// <param name="services">The service collection.</param>
    /// <returns>The <see cref="IHttpClientBuilder"/> for the Wise Old Man client.</returns>
    /// <exception cref="ArgumentNullException"><paramref name="services"/> is null.</exception>
    public static IHttpClientBuilder AddGielinomicsWiseOldManClient(this IServiceCollection services)
    {
        ArgumentNullException.ThrowIfNull(services);

        return services.AddHttpClient<IWiseOldManClient, WiseOldManClient>(WiseOldManHttpClientName, (provider, http) =>
        {
            var options = provider.GetRequiredService<IOptions<GielinomicsClientOptions>>().Value;

            http.BaseAddress = options.WiseOldManBaseAddress;
            http.Timeout = options.Timeout;
            http.DefaultRequestHeaders.UserAgent.ParseAdd(options.UserAgent);

            if (!string.IsNullOrWhiteSpace(options.WiseOldManApiKey))
            {
                http.DefaultRequestHeaders.Add("x-api-key", options.WiseOldManApiKey);
            }
        });
    }
}
