using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using Weatheria.Client.Prices;

namespace Weatheria.Client;

/// <summary>DI registration for the Weatheria clients.</summary>
public static class ServiceCollectionExtensions
{
    /// <summary>Named <see cref="HttpClient"/> for the prices API. Use this to attach your own resilience policy.</summary>
    public const string PricesHttpClientName = "weatheria.prices";

    /// <summary>
    /// Registers <see cref="IPricesClient"/> and its <see cref="HttpClient"/>.
    /// </summary>
    /// <param name="services">The service collection.</param>
    /// <param name="configure">Sets options. <see cref="WeatheriaClientOptions.UserAgent"/> is required.</param>
    /// <returns>The <see cref="IHttpClientBuilder"/> for the prices client, so callers can add resilience.</returns>
    /// <exception cref="ArgumentNullException">Any argument is null.</exception>
    public static IHttpClientBuilder AddWeatheriaClient(
        this IServiceCollection services,
        Action<WeatheriaClientOptions> configure)
    {
        ArgumentNullException.ThrowIfNull(services);
        ArgumentNullException.ThrowIfNull(configure);

        services.AddOptions<WeatheriaClientOptions>()
            .Configure(configure)
            .ValidateDataAnnotations()
            .ValidateOnStart();

        return services.AddHttpClient<IPricesClient, PricesClient>(PricesHttpClientName, (provider, http) =>
        {
            var options = provider.GetRequiredService<IOptions<WeatheriaClientOptions>>().Value;

            http.BaseAddress = options.PricesBaseAddress;
            http.Timeout = options.Timeout;
            http.DefaultRequestHeaders.UserAgent.ParseAdd(options.UserAgent);
        });
    }
}
