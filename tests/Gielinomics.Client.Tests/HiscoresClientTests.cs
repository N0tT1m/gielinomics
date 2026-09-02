using System.Net;
using Gielinomics.Client.Hiscores;
using Microsoft.Extensions.Logging;
using Xunit;

namespace Gielinomics.Client.Tests;

/// <summary>
/// Hiscores behaviour, against a body recorded from the live API on 2 September 2026.
/// </summary>
public class HiscoresClientTests
{
    [Fact]
    public async Task GetAsync_parses_the_named_skill_and_activity_blocks()
    {
        using var handler = FixtureHandler.FromFixture("hiscore-main.json");

        var profile = await handler.CreateHiscoresClient().GetAsync("Lynx Titan", HiscoreTable.Main, CancellationToken.None);

        Assert.NotNull(profile);
        Assert.Equal("Lynx Titan", profile.Name);
        Assert.Equal(25, profile.Skills.Count);

        var overall = profile.Skills[0];
        Assert.Equal("Overall", overall.Name);
        Assert.Equal(0, overall.Id);
        Assert.Equal(2278, overall.Level);
    }

    [Fact]
    public async Task GetAsync_reads_experience_beyond_int32()
    {
        using var handler = FixtureHandler.FromFixture("hiscore-main.json");

        var profile = await handler.CreateHiscoresClient().GetAsync("Lynx Titan", cancellationToken: CancellationToken.None);

        // Overall XP on a maxed account is 4.6 billion. An int would wrap it to a negative.
        Assert.Equal(4_600_000_000L, profile!.Skills[0].Xp);
        Assert.True(profile.Skills[0].Xp > int.MaxValue);
    }

    [Fact]
    public async Task GetAsync_preserves_unranked_as_minus_one_rather_than_zero()
    {
        using var handler = FixtureHandler.FromFixture("hiscore-main.json");

        var profile = await handler.CreateHiscoresClient().GetAsync("Lynx Titan", cancellationToken: CancellationToken.None);

        // An unranked activity is an absence. Normalising -1 to 0 would claim the player has
        // a rank and a score of zero, which is a different and false statement.
        var unranked = profile!.Activities.First(activity => activity.Rank < 0);
        Assert.Equal(-1, unranked.Rank);
        Assert.Equal(-1, unranked.Score);
        Assert.False(unranked.IsRanked);
    }

    [Fact]
    public async Task GetAsync_stamps_the_table_it_queried()
    {
        using var handler = FixtureHandler.FromFixture("hiscore-main.json");

        var profile = await handler.CreateHiscoresClient().GetAsync("Lynx Titan", HiscoreTable.Ironman, CancellationToken.None);

        Assert.Equal(HiscoreTable.Ironman, profile!.Table);
        Assert.Contains("m=hiscore_oldschool_ironman", handler.LastRequestUri!.AbsoluteUri, StringComparison.Ordinal);
    }

    [Fact]
    public async Task GetAsync_encodes_spaces_in_names()
    {
        using var handler = FixtureHandler.FromFixture("hiscore-main.json");

        await handler.CreateHiscoresClient().GetAsync("Lynx Titan", cancellationToken: CancellationToken.None);

        // AbsoluteUri, not ToString(): ToString() decodes escapes back for display, so it
        // would report a space and hide whether the wire form was ever encoded at all.
        // An unencoded space produces a malformed request line, not a lenient lookup.
        Assert.Contains("player=Lynx%20Titan", handler.LastRequestUri!.AbsoluteUri, StringComparison.Ordinal);
    }

    [Fact]
    public async Task GetAsync_returns_null_for_404_rather_than_throwing()
    {
        using var handler = FixtureHandler.FromBody("<html>404</html>", HttpStatusCode.NotFound, "text/html");

        var profile = await handler.CreateHiscoresClient().GetAsync("nobody", cancellationToken: CancellationToken.None);

        // 404 means "not on this table", which is a result. Detection depends on it being one.
        Assert.Null(profile);
    }

    [Fact]
    public async Task GetAsync_throws_for_other_failures()
    {
        using var handler = FixtureHandler.FromBody("down", HttpStatusCode.ServiceUnavailable, "text/plain");

        var ex = await Assert.ThrowsAsync<HiscoresApiException>(
            () => handler.CreateHiscoresClient().GetAsync("someone", cancellationToken: CancellationToken.None));

        Assert.Equal(HttpStatusCode.ServiceUnavailable, ex.StatusCode);
    }

    [Fact]
    public async Task DetectAccountTypeAsync_returns_the_most_specific_matching_table()
    {
        // An ultimate ironman appears on the ironman table too. Probing in the wrong order
        // would classify every ultimate as a plain ironman.
        using var handler = FixtureHandler.FromFixture("hiscore-main.json")
            .ServeOnlyWhen(uri => uri!.AbsoluteUri.Contains("hiscore_oldschool_ultimate", StringComparison.Ordinal)
                               || uri.AbsoluteUri.Contains("m=hiscore_oldschool/", StringComparison.Ordinal));

        var table = await handler.CreateHiscoresClient().DetectAccountTypeAsync("Someone", CancellationToken.None);

        Assert.Equal(HiscoreTable.UltimateIronman, table);
    }

    [Fact]
    public async Task DetectAccountTypeAsync_falls_through_to_main()
    {
        using var handler = FixtureHandler.FromFixture("hiscore-main.json")
            .ServeOnlyWhen(uri => uri!.AbsoluteUri.Contains("m=hiscore_oldschool/", StringComparison.Ordinal));

        var table = await handler.CreateHiscoresClient().DetectAccountTypeAsync("Lynx Titan", CancellationToken.None);

        Assert.Equal(HiscoreTable.Main, table);

        // Hardcore is probed before main, so a main costs the full walk. That is why detection
        // runs once at track time and never on a poll.
        Assert.True(handler.RequestCount > 1);
    }

    [Fact]
    public async Task DetectAccountTypeAsync_returns_null_when_no_table_has_the_player()
    {
        using var handler = FixtureHandler.FromBody("<html>404</html>", HttpStatusCode.NotFound, "text/html");

        Assert.Null(await handler.CreateHiscoresClient().DetectAccountTypeAsync("nobody", CancellationToken.None));
    }

    [Fact]
    public async Task GetAsync_stays_quiet_when_every_name_is_in_the_mapping()
    {
        var logger = new CapturingLogger<HiscoresClient>();
        using var handler = FixtureHandler.FromFixture("hiscore-main.json");

        await handler.CreateHiscoresClient(logger).GetAsync("Lynx Titan", cancellationToken: CancellationToken.None);

        // The mapping was transcribed from this very response, so an alarm here means the
        // mapping and the recorded shape have drifted apart.
        Assert.DoesNotContain(logger.Entries, entry => entry.Level == LogLevel.Error);
    }

    [Fact]
    public async Task GetAsync_alarms_when_a_response_names_something_the_mapping_does_not_know()
    {
        // A boss added by a game update, which is precisely what the CSV fallback would
        // silently misattribute every subsequent field against.
        var logger = new CapturingLogger<HiscoresClient>();
        using var handler = FixtureHandler.FromBody(
            """
            {"name":"Someone","skills":[{"id":0,"name":"Overall","rank":1,"level":2,"xp":3}],
             "activities":[{"id":0,"name":"Some Brand New Boss","rank":-1,"score":-1}]}
            """);

        await handler.CreateHiscoresClient(logger).GetAsync("Someone", cancellationToken: CancellationToken.None);

        var alarm = Assert.Single(logger.Entries, entry => entry.Level == LogLevel.Error);
        Assert.Contains("Some Brand New Boss", alarm.Message, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData(HiscoreTable.Main, "hiscore_oldschool")]
    [InlineData(HiscoreTable.Ironman, "hiscore_oldschool_ironman")]
    [InlineData(HiscoreTable.HardcoreIronman, "hiscore_oldschool_hardcore_ironman")]
    [InlineData(HiscoreTable.UltimateIronman, "hiscore_oldschool_ultimate")]
    [InlineData(HiscoreTable.SkillerDefence, "hiscore_oldschool_skiller_defence")]
    [InlineData(HiscoreTable.FreshStart, "hiscore_oldschool_fresh_start")]
    public void ToWireValue_matches_the_documented_table_names(HiscoreTable table, string expected)
        => Assert.Equal(expected, table.ToWireValue());

    [Fact]
    public void DetectionOrder_probes_specific_tables_before_general_ones()
    {
        var order = HiscoreTables.DetectionOrder;

        Assert.Equal(HiscoreTable.Main, order[^1]);
        Assert.True(
            order.ToList().IndexOf(HiscoreTable.UltimateIronman) < order.ToList().IndexOf(HiscoreTable.Ironman),
            "Ultimate must be probed before plain ironman, or every ultimate reads as an ironman.");
    }
}

/// <summary>
/// What counts as "the same standing" for snapshot dedup.
/// </summary>
public class HiscoreContentHashTests
{
    private static HiscoreProfile Profile(int rank, int level, long xp, long score = 5)
        => new()
        {
            Name = "Someone",
            Skills = [new HiscoreSkill { Id = 0, Name = "Overall", Rank = rank, Level = level, Xp = xp }],
            Activities = [new HiscoreActivity { Id = 0, Name = "Zulrah", Rank = rank, Score = score }],
        };

    [Fact]
    public void Rank_movement_alone_does_not_change_the_hash()
    {
        // The case that matters: a dormant account's rank drifts every few minutes as other
        // players pass it. If that changed the hash, the dedup would never fire and the table
        // would grow by a row per account per hour forever.
        var before = HiscoreContentHash.Compute(Profile(rank: 1000, level: 99, xp: 13_034_431));
        var after = HiscoreContentHash.Compute(Profile(rank: 1001, level: 99, xp: 13_034_431));

        Assert.Equal(before, after);
    }

    [Fact]
    public void Gaining_experience_changes_the_hash()
    {
        var before = HiscoreContentHash.Compute(Profile(rank: 1000, level: 99, xp: 13_034_431));
        var after = HiscoreContentHash.Compute(Profile(rank: 1000, level: 99, xp: 13_034_500));

        Assert.NotEqual(before, after);
    }

    [Fact]
    public void Gaining_a_level_changes_the_hash()
    {
        var before = HiscoreContentHash.Compute(Profile(rank: 1000, level: 98, xp: 13_034_431));
        var after = HiscoreContentHash.Compute(Profile(rank: 1000, level: 99, xp: 13_034_431));

        Assert.NotEqual(before, after);
    }

    [Fact]
    public void An_activity_score_moving_changes_the_hash()
    {
        var before = HiscoreContentHash.Compute(Profile(rank: 1000, level: 99, xp: 1, score: 5));
        var after = HiscoreContentHash.Compute(Profile(rank: 1000, level: 99, xp: 1, score: 6));

        Assert.NotEqual(before, after);
    }

    [Fact]
    public void Entry_ordering_does_not_change_the_hash()
    {
        // Nothing guarantees the wire preserves order. A reshuffled but identical response
        // must not read as the player having played.
        var ascending = new HiscoreProfile
        {
            Skills =
            [
                new HiscoreSkill { Id = 0, Name = "Overall", Rank = 1, Level = 2, Xp = 3 },
                new HiscoreSkill { Id = 1, Name = "Attack", Rank = 4, Level = 5, Xp = 6 },
            ],
        };

        var shuffled = new HiscoreProfile
        {
            Skills =
            [
                new HiscoreSkill { Id = 1, Name = "Attack", Rank = 4, Level = 5, Xp = 6 },
                new HiscoreSkill { Id = 0, Name = "Overall", Rank = 1, Level = 2, Xp = 3 },
            ],
        };

        Assert.Equal(HiscoreContentHash.Compute(ascending), HiscoreContentHash.Compute(shuffled));
    }
}
