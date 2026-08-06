using Microsoft.Extensions.DependencyInjection;
using Npgsql;

namespace Gielinomics.Data;

/// <summary>DI registration for the data layer.</summary>
public static class DataServiceCollectionExtensions
{
    /// <summary>Registers the Npgsql data source and the repositories.</summary>
    /// <param name="services">The service collection.</param>
    /// <param name="connectionString">Postgres connection string.</param>
    /// <returns>The service collection, for chaining.</returns>
    /// <exception cref="ArgumentException"><paramref name="connectionString"/> is null or blank.</exception>
    public static IServiceCollection AddGielinomicsData(this IServiceCollection services, string connectionString)
    {
        ArgumentNullException.ThrowIfNull(services);
        ArgumentException.ThrowIfNullOrWhiteSpace(connectionString);

        services.AddSingleton(_ =>
        {
            var builder = new NpgsqlDataSourceBuilder(connectionString);
            return builder.Build();
        });

        services.AddSingleton<PriceRepository>();
        services.AddSingleton<ItemRepository>();
        services.AddSingleton<IngestRunRepository>();

        return services;
    }
}
