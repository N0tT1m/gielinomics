using System.Net;
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

    [Fact]
    public async Task GetLatest_preserves_nulls_rather_than_coalescing_to_zero()
    {
        using var handler = FixtureHandler.FromFixture("latest.json");

        var latest = await handler.CreateClient().GetLatestAsync(CancellationToken.None);

        // Item 13652 has never had a recorded instant-buy. Zero is a price; null is an
        // absence, and collapsing the two would invent a trade that never happened.
        var dragonAxe = latest[13652];
        Assert.Null(dragonAxe.High);
        Assert.Null(dragonAxe.HighTime);
        Assert.Null(dragonAxe.HighTimeUtc);
        Assert.Equal(41_000_000, dragonAxe.Low);
    }

    [Fact]
    public async Task GetLatest_reads_prices_that_do_not_fit_a_smaller_integral_type()
    {
        using var handler = FixtureHandler.FromFixture("latest.json");

        var latest = await handler.CreateClient().GetLatestAsync(CancellationToken.None);

        // Item 22486 trades in the billions. These have to survive as longs; a narrower
        // type silently truncates at the top of the market, where the money is.
        var scythe = latest[22486];
        Assert.Equal(1_284_000_000L, scythe.High);
        Assert.Equal(1_270_000_000L, scythe.Low);
    }

    [Fact]
    public async Task GetLatest_for_one_item_returns_null_when_the_response_has_no_row()
    {
        using var handler = FixtureHandler.FromFixture("latest.json");

        var missing = await handler.CreateClient().GetLatestAsync(999_999, CancellationToken.None);

        Assert.Null(missing);
        Assert.Contains("id=999999", handler.LastRequestUri!.Query, StringComparison.Ordinal);
    }

    [Fact]
    public async Task GetMapping_tolerates_items_with_no_buy_limit()
    {
        using var handler = FixtureHandler.FromFixture("mapping.json");

        var mapping = await handler.CreateClient().GetMappingAsync(CancellationToken.None);

        // Coins carry no "limit" field at all. An absent limit is not a limit of zero.
        var coins = mapping.Single(item => item.Id == 617);
        Assert.Null(coins.Limit);
        Assert.False(coins.Members);

        var whip = mapping.Single(item => item.Id == 4151);
        Assert.Equal(8, whip.Limit);
        Assert.Equal(72_000, whip.HighAlch);
    }

    [Fact]
    public async Task GetMapping_captures_fields_the_model_does_not_know_about()
    {
        // The schema drift alarm depends on unmodelled fields surviving deserialisation
        // rather than being silently dropped.
        using var handler = FixtureHandler.FromBody(
            """[{"id":1,"name":"Test","members":false,"someBrandNewField":42}]""");

        var mapping = await handler.CreateClient().GetMappingAsync(CancellationToken.None);

        Assert.NotNull(mapping[0].AdditionalData);
        Assert.Contains("someBrandNewField", mapping[0].AdditionalData!.Keys);
    }

    [Fact]
    public async Task Get5m_reads_the_window_start_from_the_response()
    {
        using var handler = FixtureHandler.FromFixture("5m.json");

        var envelope = await handler.CreateClient().Get5mAsync(cancellationToken: CancellationToken.None);

        Assert.Equal(1_785_974_400L, envelope.Timestamp);
        Assert.Equal(3, envelope.Data.Count);
        Assert.Equal(186.5m, envelope.Data[2].AvgHighPrice);

        // A window in which nothing traded reports null prices with zero volume.
        Assert.Null(envelope.Data[13652].AvgHighPrice);
        Assert.Equal(0, envelope.Data[13652].HighPriceVolume);

        // No timestamp asked for means "the most recently completed window".
        Assert.Equal(string.Empty, handler.LastRequestUri!.Query);
    }

    [Fact]
    public async Task Get5m_passes_an_explicit_timestamp_for_gap_repair()
    {
        using var handler = FixtureHandler.FromFixture("5m.json");
        var window = DateTimeOffset.FromUnixTimeSeconds(1_785_974_400);

        await handler.CreateClient().Get5mAsync(window, CancellationToken.None);

        // The timestamp parameter is the entire mechanism behind gap repair. If it stops
        // being sent, repair silently re-fetches the newest window over and over.
        Assert.Contains("timestamp=1785974400", handler.LastRequestUri!.Query, StringComparison.Ordinal);
        Assert.Contains("/5m", handler.LastRequestUri.AbsolutePath, StringComparison.Ordinal);
    }

    [Fact]
    public async Task GetTimeSeries_reports_daily_step_for_a_one_year_lookback()
    {
        using var handler = FixtureHandler.FromFixture("timeseries-1y.json");

        var series = await handler.CreateClient()
            .GetTimeSeriesAsync(4151, Lookback.OneYear, CancellationToken.None);

        // The single most important fact about this route: a year of lookback is 365 daily
        // bars, not 5-minute ones. Persist this step, never the one the lookback implies.
        Assert.Equal(86_400, series.TimeStep);
        Assert.Equal(4151, series.ItemId);
        Assert.Equal(3, series.Data.Count);
        Assert.Equal(DateTimeOffset.FromUnixTimeSeconds(1_754_438_400), series.Data[0].TimestampUtc);

        Assert.Contains("lookback=1y", handler.LastRequestUri!.Query, StringComparison.Ordinal);
        Assert.Contains("id=4151", handler.LastRequestUri.Query, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData(Lookback.SixHours, "6h")]
    [InlineData(Lookback.OneDay, "24h")]
    [InlineData(Lookback.OneWeek, "7d")]
    [InlineData(Lookback.OneMonth, "30d")]
    [InlineData(Lookback.SixMonths, "6m")]
    [InlineData(Lookback.OneYear, "1y")]
    public async Task GetTimeSeries_sends_the_wire_value_for_each_lookback(Lookback lookback, string expected)
    {
        using var handler = FixtureHandler.FromFixture("timeseries-1y.json");

        await handler.CreateClient().GetTimeSeriesAsync(4151, lookback, CancellationToken.None);

        Assert.Contains($"lookback={expected}", handler.LastRequestUri!.Query, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData(HttpStatusCode.NotFound, false)]
    [InlineData(HttpStatusCode.TooManyRequests, true)]
    [InlineData(HttpStatusCode.ServiceUnavailable, true)]
    [InlineData(HttpStatusCode.Forbidden, false)]
    public async Task Non_success_responses_surface_as_PricesApiException(HttpStatusCode status, bool transient)
    {
        using var handler = FixtureHandler.FromBody("nope", status, "text/plain");

        var ex = await Assert.ThrowsAsync<PricesApiException>(
            () => handler.CreateClient().GetLatestAsync(CancellationToken.None));

        Assert.Equal(status, ex.StatusCode);
        Assert.Equal(transient, ex.IsTransient);
    }

    [Fact]
    public async Task Unparseable_bodies_surface_as_PricesApiException_wrapping_the_json_error()
    {
        // The distinction matters to the worker: a wrapped JsonException is recorded as a
        // parse_error, which points at a schema change rather than at an outage.
        using var handler = FixtureHandler.FromBody("{ this is not json");

        var ex = await Assert.ThrowsAsync<PricesApiException>(
            () => handler.CreateClient().GetLatestAsync(CancellationToken.None));

        Assert.IsType<System.Text.Json.JsonException>(ex.InnerException);
        Assert.False(ex.IsTransient);
    }
}
