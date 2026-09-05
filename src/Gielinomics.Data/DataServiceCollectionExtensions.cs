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

        // Before any repository is resolved: Dapper caches a materialiser per (type, query)
        // the first time it sees one, and a handler registered after that cache is warm has
        // no effect on the models already cached.
        DapperTypeHandlers.Register();

        services.AddSingleton(_ =>
        {
            var builder = new NpgsqlDataSourceBuilder(connectionString);
            return builder.Build();
        });

        services.AddSingleton<PriceRepository>();
        services.AddSingleton<ItemRepository>();
        services.AddSingleton<IngestRunRepository>();

        // Read side. Registered here too so the API and the worker share one data source
        // and therefore one connection pool.
        services.AddSingleton<MarketQueryRepository>();
        services.AddSingleton<IngestQueryRepository>();
        services.AddSingleton<ApiUserRepository>();
        services.AddSingleton<IApiUserLookup>(sp => sp.GetRequiredService<ApiUserRepository>());
        services.AddSingleton<AlertRepository>();
        services.AddSingleton<PlayerRepository>();
        services.AddSingleton<WikiRepository>();

        return services;
    }
}
