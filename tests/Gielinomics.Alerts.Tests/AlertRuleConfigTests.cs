using Gielinomics.Alerts;
using Xunit;

namespace Gielinomics.Alerts.Tests;

/// <summary>
/// Write-time validation of <c>alert_rules.config</c>.
/// </summary>
/// <remarks>
/// Every rejection here is one the POST has to make, because the alternative is the
/// evaluator discovering it hours later on a sweep nobody is watching.
/// </remarks>
public sealed class AlertRuleConfigTests
{
    [Fact]
    public void Accepts_a_well_formed_margin_rule()
    {
        var ok = AlertRuleConfig.TryValidate(
            AlertRuleKind.Margin,
            """{"itemId":4151,"minNetMargin":50000,"minVolume":100}""",
            out var error);

        Assert.True(ok);
        Assert.Null(error);
    }

    [Fact]
    public void Accepts_a_margin_rule_without_a_volume_floor()
    {
        // minVolume is optional in the config record; omitting it must not be a parse failure.
        var ok = AlertRuleConfig.TryValidate(
            AlertRuleKind.Margin,
            """{"itemId":4151,"minNetMargin":1}""",
            out var error);

        Assert.True(ok);
        Assert.Null(error);
    }

    [Fact]
    public void Accepts_a_well_formed_volume_rule()
    {
        var ok = AlertRuleConfig.TryValidate(
            AlertRuleKind.Volume,
            """{"itemId":561,"minVolume":250000,"windowHours":6}""",
            out var error);

        Assert.True(ok);
        Assert.Null(error);
    }

    [Fact]
    public void Rejects_an_unknown_kind()
    {
        var ok = AlertRuleConfig.TryValidate("xp_milestone", """{"itemId":1}""", out var error);

        Assert.False(ok);
        Assert.Contains("xp_milestone", error, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("""{"itemId":4151,"minNetMargin":0}""")]
    [InlineData("""{"itemId":4151,"minNetMargin":-1}""")]
    public void Rejects_a_margin_that_cannot_fire_usefully(string config)
    {
        // A non-positive threshold fires on every sweep for every item, which is the same
        // as having no rule except that it also spams the webhook.
        var ok = AlertRuleConfig.TryValidate(AlertRuleKind.Margin, config, out var error);

        Assert.False(ok);
        Assert.Contains("minNetMargin", error, StringComparison.Ordinal);
    }

    [Fact]
    public void Rejects_a_negative_volume_floor_on_a_margin_rule()
    {
        var ok = AlertRuleConfig.TryValidate(
            AlertRuleKind.Margin,
            """{"itemId":4151,"minNetMargin":10,"minVolume":-5}""",
            out var error);

        Assert.False(ok);
        Assert.Contains("minVolume", error, StringComparison.Ordinal);
    }

    [Fact]
    public void Rejects_a_non_positive_volume_threshold()
    {
        var ok = AlertRuleConfig.TryValidate(
            AlertRuleKind.Volume,
            """{"itemId":561,"minVolume":0}""",
            out var error);

        Assert.False(ok);
        Assert.Contains("minVolume", error, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData(0)]
    [InlineData(169)]
    public void Rejects_a_volume_window_outside_one_hour_to_one_week(int windowHours)
    {
        var ok = AlertRuleConfig.TryValidate(
            AlertRuleKind.Volume,
            $$"""{"itemId":561,"minVolume":10,"windowHours":{{windowHours}}}""",
            out var error);

        Assert.False(ok);
        Assert.Contains("windowHours", error, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData(1)]
    [InlineData(168)]
    public void Accepts_the_window_bounds_themselves(int windowHours)
    {
        var ok = AlertRuleConfig.TryValidate(
            AlertRuleKind.Volume,
            $$"""{"itemId":561,"minVolume":10,"windowHours":{{windowHours}}}""",
            out var error);

        Assert.True(ok, error);
    }

    [Fact]
    public void Rejects_malformed_json_rather_than_throwing()
    {
        // The caller controls this string. A JsonException escaping TryValidate would be a
        // 500 on a request that deserves a 400.
        var ok = AlertRuleConfig.TryValidate(AlertRuleKind.Margin, "{ not json", out var error);

        Assert.False(ok);
        Assert.NotNull(error);
    }

    [Fact]
    public void Rejects_a_json_literal_null()
    {
        var ok = AlertRuleConfig.TryValidate(AlertRuleKind.Volume, "null", out var error);

        Assert.False(ok);
        Assert.Contains("JSON object", error, StringComparison.Ordinal);
    }

    [Fact]
    public void Rejects_a_margin_config_missing_its_required_members()
    {
        var ok = AlertRuleConfig.TryValidate(AlertRuleKind.Margin, "{}", out var error);

        Assert.False(ok);
        Assert.NotNull(error);
    }
}
