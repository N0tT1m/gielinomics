using System.Globalization;
using System.Text.Json;
using Gielinomics.Data;
using Microsoft.Extensions.Logging;

namespace Gielinomics.Alerts;

/// <summary>Options governing how alert rules are evaluated.</summary>
public sealed class AlertEvaluatorOptions
{
    /// <summary>How often the evaluator sweeps every enabled rule.</summary>
    public TimeSpan SweepInterval { get; set; } = TimeSpan.FromMinutes(5);

    /// <summary>
    /// Minimum time between two firings of the same rule.
    /// </summary>
    /// <remarks>
    /// A margin rule whose condition holds for an hour would otherwise fire on every sweep.
    /// The cooldown is what makes the alert a signal rather than a stream.
    /// </remarks>
    public TimeSpan Cooldown { get; set; } = TimeSpan.FromHours(1);

    /// <summary>How recent a latest-price observation must be to be worth alerting on.</summary>
    public TimeSpan PriceFreshness { get; set; } = TimeSpan.FromHours(1);
}

/// <summary>
/// Evaluates every enabled alert rule and dispatches the ones that fire.
/// </summary>
/// <param name="alerts">Rule storage.</param>
/// <param name="market">Market reads.</param>
/// <param name="dispatcher">Where fired alerts go.</param>
/// <param name="taxRules">Supplies the tax rules in force at evaluation time.</param>
/// <param name="options">Evaluation options.</param>
/// <param name="logger">Log sink.</param>
public sealed class AlertEvaluator(
    AlertRepository alerts,
    MarketQueryRepository market,
    IAlertDispatcher dispatcher,
    TaxRulesProvider taxRules,
    AlertEvaluatorOptions options,
    ILogger<AlertEvaluator> logger)
{
    private readonly AlertRepository _alerts = alerts;
    private readonly MarketQueryRepository _market = market;
    private readonly IAlertDispatcher _dispatcher = dispatcher;
    // The provider, not a snapshot of its contents: the exempt set is resolved from the
    // items table after startup, and a captured value would keep over-charging tax forever.
    private readonly TaxRulesProvider _taxRules = taxRules;
    private readonly AlertEvaluatorOptions _options = options;
    private readonly ILogger<AlertEvaluator> _logger = logger;

    /// <summary>Evaluates every enabled rule once.</summary>
    /// <param name="cancellationToken">Cancels the sweep.</param>
    /// <returns>How many rules fired.</returns>
    public async Task<int> EvaluateAllAsync(CancellationToken cancellationToken = default)
    {
        var rules = await _alerts.ListEnabledAsync(cancellationToken).ConfigureAwait(false);
        var fired = 0;

        foreach (var rule in rules)
        {
            cancellationToken.ThrowIfCancellationRequested();

            if (rule.LastFired is { } last && DateTimeOffset.UtcNow - last < _options.Cooldown)
            {
                continue;
            }

            try
            {
                var notification = await EvaluateAsync(rule, cancellationToken).ConfigureAwait(false);
                if (notification is null)
                {
                    continue;
                }

                if (await _dispatcher.DispatchAsync(rule.WebhookUrl, notification, cancellationToken).ConfigureAwait(false))
                {
                    await _alerts.MarkFiredAsync(rule.Id, cancellationToken).ConfigureAwait(false);
                    fired++;
                }
            }
            catch (JsonException ex)
            {
                // A rule stored before its config shape was tightened, or written straight to
                // the table. Log and skip: one bad row must not stop the sweep.
                _logger.LogError(ex, "Alert rule {RuleId} has unreadable configuration; skipping.", rule.Id);
            }
        }

        return fired;
    }

    /// <summary>Evaluates one rule.</summary>
    /// <param name="rule">The rule.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The notification to send, or null when the rule did not fire.</returns>
    private async Task<AlertNotification?> EvaluateAsync(AlertRule rule, CancellationToken cancellationToken)
        => rule.Kind switch
        {
            AlertRuleKind.Margin => await EvaluateMarginAsync(rule, cancellationToken).ConfigureAwait(false),
            AlertRuleKind.Volume => await EvaluateVolumeAsync(rule, cancellationToken).ConfigureAwait(false),
            _ => null,
        };

    /// <summary>Evaluates a tax-adjusted margin rule.</summary>
    /// <param name="rule">The rule.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The notification, or null.</returns>
    private async Task<AlertNotification?> EvaluateMarginAsync(AlertRule rule, CancellationToken cancellationToken)
    {
        var config = JsonSerializer.Deserialize<MarginRuleConfig>(rule.Config, AlertRuleConfig.SerializerOptions);
        if (config is null)
        {
            return null;
        }

        var spread = await _market
            .GetSpreadForItemAsync(config.ItemId, _options.PriceFreshness, cancellationToken)
            .ConfigureAwait(false);

        if (spread is null)
        {
            return null;
        }

        // The thinner side, not the total: you have to both buy and sell to realise a flip,
        // and the side with less volume is the one that will not fill.
        var tradableVolume = Math.Min(spread.HighVolume, spread.LowVolume);
        if (tradableVolume < config.MinVolume)
        {
            return null;
        }

        // Buy at the instant-sell price, sell at the instant-buy price. Tax lands on the sale.
        var rules = _taxRules.Current;
        var margin = rules.NetMargin(config.ItemId, spread.Low, spread.High);
        if (margin < config.MinNetMargin)
        {
            return null;
        }

        var name = spread.Name ?? FormattableString.Invariant($"Item {config.ItemId}");
        var limitText = spread.BuyLimit is { } limit
            ? string.Create(CultureInfo.InvariantCulture, $"{limit:N0} per 4h")
            : "no published limit";

        return new AlertNotification(
            $"{name}: {margin:N0} gp margin",
            string.Create(CultureInfo.InvariantCulture, $"""
                Buy at {spread.Low:N0}, sell at {spread.High:N0}.
                Net {margin:N0} gp/unit after {rules.Rate:P0} tax.
                Buy limit {limitText}; potential {margin * (spread.BuyLimit ?? 1):N0} gp per limit window.
                Volume in the last {_options.PriceFreshness.TotalHours:0.#}h — buy {spread.HighVolume:N0}, sell {spread.LowVolume:N0}.
                """));
    }

    /// <summary>Evaluates a traded-volume rule.</summary>
    /// <param name="rule">The rule.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The notification, or null.</returns>
    private async Task<AlertNotification?> EvaluateVolumeAsync(AlertRule rule, CancellationToken cancellationToken)
    {
        var config = JsonSerializer.Deserialize<VolumeRuleConfig>(rule.Config, AlertRuleConfig.SerializerOptions);
        if (config is null)
        {
            return null;
        }

        var from = DateTimeOffset.UtcNow.AddHours(-config.WindowHours);
        var stats = await _market
            .GetStatsAsync(config.ItemId, stepSeconds: 300, from, cancellationToken)
            .ConfigureAwait(false);

        var total = stats.HighVolume + stats.LowVolume;
        if (total < config.MinVolume)
        {
            return null;
        }

        var item = await _market.GetItemAsync(config.ItemId, cancellationToken).ConfigureAwait(false);
        var name = item?.Name ?? FormattableString.Invariant($"Item {config.ItemId}");

        return new AlertNotification(
            $"{name}: {total:N0} units traded",
            string.Create(CultureInfo.InvariantCulture, $"""
                {total:N0} units over the last {config.WindowHours}h, against a threshold of {config.MinVolume:N0}.
                Bought {stats.HighVolume:N0}, sold {stats.LowVolume:N0}.
                """));
    }
}
