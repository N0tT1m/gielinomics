using Gielinomics.Data;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Gielinomics.Alerts;

/// <summary>
/// Keeps the tax exempt set in step with the item mapping.
/// </summary>
/// <remarks>
/// <para>
/// Resolution happens against the <c>items</c> table rather than in memory, so every process
/// that needs tax rules — the API and the worker alike — arrives at the same answer without
/// either having to tell the other anything.
/// </para>
/// <para>
/// Until the first refresh lands, the rules carry an empty exempt set, which over-charges tax
/// on exempt items. That is the safe direction: a margin estimate that is too pessimistic
/// costs a missed flip, one that is too optimistic costs gp.
/// </para>
/// </remarks>
/// <param name="market">Market reads.</param>
/// <param name="provider">Where the resolved rules are published.</param>
/// <param name="logger">Log sink.</param>
public sealed class TaxExemptionRefresher(
    MarketQueryRepository market,
    TaxRulesProvider provider,
    ILogger<TaxExemptionRefresher> logger) : BackgroundService
{
    /// <summary>How often the exempt set is re-resolved.</summary>
    /// <remarks>The mapping sync runs daily; refreshing twice as often is enough to track it.</remarks>
    private static readonly TimeSpan RefreshInterval = TimeSpan.FromHours(12);

    private readonly MarketQueryRepository _market = market;
    private readonly TaxRulesProvider _provider = provider;
    private readonly ILogger<TaxExemptionRefresher> _logger = logger;

    /// <inheritdoc />
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        using var timer = new PeriodicTimer(RefreshInterval);

        do
        {
            try
            {
                var items = await _market.GetItemNamesAsync(stoppingToken).ConfigureAwait(false);
                var resolved = GrandExchangeTaxRules.Current
                    .ResolveExemptions(items.Select(item => (item.Id, item.Name)));

                _provider.Update(resolved);

                _logger.LogInformation(
                    "Resolved {ExemptCount} tax-exempt item IDs from {ItemCount} named items.",
                    resolved.ExemptItemIds.Count,
                    items.Count);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                return;
            }
            catch (Exception ex)
            {
                // Keep the previous rules and try again next tick. A database blip must not
                // take the margin scan offline.
                _logger.LogError(ex, "Failed to resolve tax exemptions; keeping the previous set.");
            }
        }
        while (await timer.WaitForNextTickAsync(stoppingToken).ConfigureAwait(false));
    }
}
