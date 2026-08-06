using Gielinomics.Client.Prices;
using Xunit;

namespace Gielinomics.Client.Tests;

/// <summary>
/// Tests run against the recorded bodies in <c>Fixtures/</c>. No network in CI —
/// the suite must not depend on the wiki being up or its prices being any given value.
/// </summary>
public class PricesClientTests
{
    [Fact]
    public void Constructor_throws_when_no_user_agent_is_set()
    {
        using var http = new HttpClient { BaseAddress = new Uri("https://prices.runescape.wiki/api/v2/osrs/") };

        var ex = Assert.Throws<InvalidOperationException>(() => new PricesClient(http));
        Assert.Contains("User-Agent", ex.Message, StringComparison.Ordinal);
    }

    // TODO, once PricesClient is implemented. Each of these covers a documented gotcha:
    //
    //   GetLatest_preserves_nulls_rather_than_coalescing_to_zero   (fixture: latest.json, item 13652)
    //   GetLatest_handles_prices_above_int32                       (fixture: latest.json, item 22486)
    //   GetMapping_tolerates_items_with_no_buy_limit               (fixture: mapping.json, item 617)
    //   Get5m_reads_the_window_start_from_the_response             (fixture: 5m.json)
    //   Get5m_passes_an_explicit_timestamp_for_gap_repair          (assert on the query string)
    //   GetTimeSeries_reports_daily_step_for_a_one_year_lookback   (fixture: timeseries-1y.json, timestep 86400)
    //   Non_success_responses_surface_as_PricesApiException
    //
    // A FixtureHandler : HttpMessageHandler that reads Fixtures/{name} and records the
    // last request URI covers all of them.
}
