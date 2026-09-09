using Gielinomics.Api.Endpoints;
using Gielinomics.Api.Infrastructure;
using Xunit;

namespace Gielinomics.Api.Tests;

/// <summary>
/// The window table behind <c>/api/prices/{window}</c>.
/// </summary>
/// <remarks>
/// A table of constants is worth a test only when getting one wrong fails quietly, and this is
/// that case twice over. A window mapped to a granularity the ingest workers never write
/// returns an empty result set, which reads on the wire as "nothing traded" rather than as a
/// misconfiguration; and a window whose span is shorter than its own bars aggregates one bar or
/// none, which reads as a real but wrong average. Neither throws, so neither is noticed.
/// </remarks>
public class PriceMirrorWindowTests
{
    /// <summary>Granularities the ingest workers actually write to <c>price_series</c>.</summary>
    /// <remarks>
    /// Duplicated from <c>db/init/01_schema.sql</c> rather than imported, deliberately: the
    /// point is to notice when the two drift apart, and a shared constant cannot.
    /// </remarks>
    private static readonly int[] StoredSteps = [300, 3600, 86_400];

    [Theory]
    [InlineData("5m")]
    [InlineData("1h")]
    [InlineData("6h")]
    [InlineData("24h")]
    public void Every_window_upstream_serves_is_served_here(string window)
        => Assert.True(PriceMirrorEndpoints.Windows.ContainsKey(window));

    [Fact]
    public void Every_window_aggregates_a_granularity_that_is_actually_stored()
    {
        foreach (var (name, resolved) in PriceMirrorEndpoints.Windows)
        {
            Assert.True(
                Array.IndexOf(StoredSteps, resolved.StepSeconds) >= 0,
                $"Window '{name}' aggregates {resolved.StepSeconds}s bars, which nothing writes.");
        }
    }

    [Fact]
    public void No_window_is_shorter_than_the_bars_it_aggregates()
    {
        foreach (var (name, resolved) in PriceMirrorEndpoints.Windows)
        {
            Assert.True(
                resolved.Window.TotalSeconds >= resolved.StepSeconds,
                $"Window '{name}' spans {resolved.Window.TotalSeconds}s of {resolved.StepSeconds}s bars.");
        }
    }

    [Theory]
    [InlineData("24H")]
    [InlineData("24h")]
    [InlineData("6H")]
    public void Window_names_are_matched_case_insensitively(string window)
    {
        // Upstream's own routes are lower case, but a client building the path from a label
        // it displayed to somebody should not 400 over the shift key.
        Assert.True(PriceMirrorEndpoints.Windows.ContainsKey(window));
    }

    [Fact]
    public void An_unknown_window_is_absent_rather_than_defaulted()
    {
        // The route turns this into a 400. Silently falling back to 24h would answer a
        // question about a period the caller did not ask for, which is worse than refusing.
        Assert.False(PriceMirrorEndpoints.Windows.ContainsKey("7d"));
        Assert.False(PriceMirrorEndpoints.Windows.ContainsKey(""));
    }

    [Fact]
    public void The_mirror_and_the_platforms_own_routes_agree_on_what_a_name_means()
    {
        // "5m" and "1h" appear in both vocabularies. They are separate tables on purpose --
        // one is upstream's contract, one is this platform's -- but a name that means five
        // minutes on one route and something else on the other is a trap, not a design.
        foreach (var (name, resolved) in PriceMirrorEndpoints.Windows)
        {
            if (!QueryConventions.Intervals.TryGetValue(name, out var interval))
            {
                continue;
            }

            Assert.Equal(interval, (int)resolved.Window.TotalSeconds);
        }
    }
}
