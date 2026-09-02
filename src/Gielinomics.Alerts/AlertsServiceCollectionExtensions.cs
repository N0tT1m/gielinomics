using Microsoft.Extensions.DependencyInjection;

namespace Gielinomics.Alerts;

/// <summary>DI registration for the alerting layer.</summary>
public static class AlertsServiceCollectionExtensions
{
    /// <summary>Named <see cref="HttpClient"/> used to reach Discord.</summary>
    public const string DispatcherHttpClientName = "gielinomics.alerts.discord";

    /// <summary>
    /// Registers the evaluator, the Discord dispatcher, and the tax rules.
    /// </summary>
    /// <remarks>
    /// Consumers take <see cref="TaxRulesProvider"/> rather than <see cref="GrandExchangeTaxRules"/>
    /// directly. The exempt set is resolved from the items table after startup, so a captured
    /// snapshot would keep the empty set — and therefore over-charge tax — for the life of the
    /// process.
    /// </remarks>
    /// <param name="services">The service collection.</param>
    /// <param name="configure">Optional evaluator tuning.</param>
    /// <returns>The service collection, for chaining.</returns>
    /// <exception cref="ArgumentNullException"><paramref name="services"/> is null.</exception>
    public static IServiceCollection AddGielinomicsAlerts(
        this IServiceCollection services,
        Action<AlertEvaluatorOptions>? configure = null)
    {
        ArgumentNullException.ThrowIfNull(services);

        var options = new AlertEvaluatorOptions();
        configure?.Invoke(options);
        services.AddSingleton(options);

        services.AddSingleton<TaxRulesProvider>();
        services.AddHostedService<TaxExemptionRefresher>();

        services.AddHttpClient<IAlertDispatcher, DiscordWebhookDispatcher>(DispatcherHttpClientName, http =>
        {
            http.Timeout = TimeSpan.FromSeconds(10);
            http.DefaultRequestHeaders.UserAgent.ParseAdd("gielinomics-alerts/0.1");
        });

        services.AddSingleton<AlertEvaluator>();

        return services;
    }
}

/// <summary>
/// Holds the tax rules currently in force, so the mapping sync can replace the exempt set
/// without every consumer having to re-resolve it.
/// </summary>
public sealed class TaxRulesProvider
{
    private GrandExchangeTaxRules _current = GrandExchangeTaxRules.Current;

    /// <summary>The rules in force.</summary>
    public GrandExchangeTaxRules Current => Volatile.Read(ref _current);

    /// <summary>Whether the exempt set has been resolved from a live mapping yet.</summary>
    public bool ExemptionsResolved => Current.ExemptItemIds.Count > 0;

    /// <summary>Replaces the rules, typically after a mapping sync.</summary>
    /// <param name="rules">The new rules.</param>
    /// <exception cref="ArgumentNullException"><paramref name="rules"/> is null.</exception>
    public void Update(GrandExchangeTaxRules rules)
    {
        ArgumentNullException.ThrowIfNull(rules);
        Volatile.Write(ref _current, rules);
    }
}
