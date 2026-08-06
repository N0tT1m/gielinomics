using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using Gielinomics.Client.Prices;

namespace Gielinomics.Client;

/// <summary>DI registration for the Gielinomics clients.</summary>
public static class ServiceCollectionExtensions
{
    /// <summary>Named <see cref="HttpClient"/> for the prices API. Use this to attach your own resilience policy.</summary>
    public const string PricesHttpClientName = "gielinomics.prices";

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
}
