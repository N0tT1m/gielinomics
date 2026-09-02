using Gielinomics.Ingest.Workers;
using Xunit;

namespace Gielinomics.Ingest.Tests;

/// <summary>
/// The window arithmetic behind polling and gap repair.
/// </summary>
/// <remarks>
/// Off-by-one errors here do not throw. They produce a dataset that is quietly missing every
/// other window, or one whose bars are stored against the wrong hour — which is exactly the
/// untrustworthy dataset the project exists to avoid.
/// </remarks>
public class PriceSeriesSchedulingTests
{
    private static readonly DateTimeOffset Epoch = DateTimeOffset.FromUnixTimeSeconds(0);

    [Theory]
    [InlineData(0, 300, 0)]
    [InlineData(299, 300, 0)]
    [InlineData(300, 300, 300)]
    [InlineData(301, 300, 300)]
    [InlineData(3_599, 3_600, 0)]
    [InlineData(3_600, 3_600, 3_600)]
    public void FloorToStep_rounds_down_to_the_window_start(long unixSeconds, int step, long expected)
    {
        var floored = PriceSeriesWorker.FloorToStep(DateTimeOffset.FromUnixTimeSeconds(unixSeconds), step);

        Assert.Equal(DateTimeOffset.FromUnixTimeSeconds(expected), floored);
    }

    [Fact]
    public void LastCompletedBoundary_excludes_the_window_still_in_progress()
    {
        var worker = CreateWorker(PriceSeriesWorker.Feed.FiveMinute);

        // At 10:07, the 10:05 window is still open. The newest closed window starts at 10:00.
        var now = Epoch.AddHours(10).AddMinutes(7);
        Assert.Equal(Epoch.AddHours(10), worker.LastCompletedBoundary(now));

        // Exactly on a boundary, the window that just opened is still not complete.
        Assert.Equal(Epoch.AddHours(10).AddMinutes(-5), worker.LastCompletedBoundary(Epoch.AddHours(10)));
    }

    [Fact]
    public void NextPollAt_lands_just_after_the_boundary_so_the_window_has_closed()
    {
        var worker = CreateWorker(PriceSeriesWorker.Feed.FiveMinute);

        // 30 seconds past the boundary: polling at the boundary itself races the upstream
        // aggregation and reads an empty or partial window.
        Assert.Equal(
            Epoch.AddMinutes(5).AddSeconds(30),
            worker.NextPollAt(Epoch.AddMinutes(1)));
    }

    [Fact]
    public void NextPollAt_skips_to_the_following_window_once_this_one_is_past()
    {
        var worker = CreateWorker(PriceSeriesWorker.Feed.FiveMinute);

        // At 00:05:40 the 00:05:30 slot has already gone by. Returning it would produce a
        // negative delay and spin the loop.
        var next = worker.NextPollAt(Epoch.AddMinutes(5).AddSeconds(40));

        Assert.Equal(Epoch.AddMinutes(10).AddSeconds(30), next);
        Assert.True(next > Epoch.AddMinutes(5).AddSeconds(40));
    }

    [Fact]
    public void NextPollAt_is_always_in_the_future()
    {
        var worker = CreateWorker(PriceSeriesWorker.Feed.Hourly);

        for (var offsetSeconds = 0; offsetSeconds < 7_200; offsetSeconds += 37)
        {
            var now = Epoch.AddSeconds(offsetSeconds);
            Assert.True(worker.NextPollAt(now) > now, $"NextPollAt was not in the future at +{offsetSeconds}s.");
        }
    }

    [Fact]
    public void Feed_definitions_match_the_documented_cadences()
    {
        var fiveMinute = PriceSeriesWorker.Feed.FiveMinute;
        Assert.Equal("5m", fiveMinute.Source);
        Assert.Equal(300, fiveMinute.StepSeconds);
        Assert.Equal(TimeSpan.FromMinutes(5), fiveMinute.Interval);

        var hourly = PriceSeriesWorker.Feed.Hourly;
        Assert.Equal("1h", hourly.Source);
        Assert.Equal(3_600, hourly.StepSeconds);
        Assert.Equal(TimeSpan.FromHours(1), hourly.Interval);
    }

    /// <summary>
    /// Builds a worker for its arithmetic alone.
    /// </summary>
    /// <remarks>
    /// The scheduling helpers touch none of the collaborators, so nulls are safe here and
    /// keep the test from needing a database or an HTTP stack to check a subtraction.
    /// </remarks>
    /// <param name="feed">The feed to configure.</param>
    /// <returns>The worker.</returns>
    private static PriceSeriesWorker CreateWorker(PriceSeriesWorker.Feed feed)
        => new(feed, null!, null!, null!, null!);
}
