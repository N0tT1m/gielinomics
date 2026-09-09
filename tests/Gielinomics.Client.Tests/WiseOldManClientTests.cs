using System.Net;
using Gielinomics.Client.Hiscores;
using Gielinomics.Client.WiseOldMan;
using Xunit;

namespace Gielinomics.Client.Tests;

/// <summary>
/// Wise Old Man behaviour, against bodies recorded from the live v2 API on 5 September 2026.
/// </summary>
/// <remarks>
/// The recorded bodies keep every metric family and every skill, but sample the boss and
/// activity blocks — the full player response carries 71 bosses of identical shape. The sample
/// always retains an unranked entry, because the <c>-1</c> sentinel is the part most likely to
/// be broken by a change here.
/// </remarks>
public class WiseOldManClientTests
{
    [Fact]
    public async Task GetPlayerAsync_parses_the_player_and_its_latest_snapshot()
    {
        using var handler = FixtureHandler.FromFixture("wom-player.json");

        var player = await handler.CreateWiseOldManClient().GetPlayerAsync("Psikoi", CancellationToken.None);

        Assert.NotNull(player);
        Assert.Equal("Psikoi", player.DisplayName);
        Assert.Equal("psikoi", player.Username);
        Assert.Equal("regular", player.Type);
        Assert.Equal(125, player.CombatLevel);

        Assert.NotNull(player.LatestSnapshot);
        Assert.Equal(25, player.LatestSnapshot.Data.Skills.Count);
        Assert.Equal(322_896_484L, player.LatestSnapshot.Data.Skills["overall"].Experience);
        Assert.Equal(2293, player.LatestSnapshot.Data.Skills["overall"].Level);
    }

    [Fact]
    public async Task GetPlayerAsync_reads_experience_beyond_int32()
    {
        // A maxed account, which the recorded player is not. Overall experience tops out at
        // 4.8 billion; an int would wrap that to a negative, so the field has to be a long.
        const string body = """
            {
              "id": 1, "username": "lynx titan", "displayName": "Lynx Titan", "type": "regular",
              "build": "main", "status": "active", "patron": false, "exp": 4800000000,
              "ehp": 0, "ehb": 0, "ttm": 0, "tt200m": 0,
              "registeredAt": "2020-01-01T00:00:00.000Z",
              "latestSnapshot": {
                "id": 1, "playerId": 1, "createdAt": "2026-01-01T00:00:00.000Z", "importedAt": null,
                "data": {
                  "bosses": {}, "activities": {}, "computed": {},
                  "skills": { "overall": { "metric": "overall", "experience": 4800000000, "rank": 1, "level": 2277, "ehp": 0 } }
                }
              }
            }
            """;

        using var handler = FixtureHandler.FromBody(body);

        var player = await handler.CreateWiseOldManClient().GetPlayerAsync("Lynx Titan", CancellationToken.None);

        Assert.Equal(4_800_000_000L, player!.Exp);
        Assert.Equal(4_800_000_000L, player.LatestSnapshot!.Data.Skills["overall"].Experience);
    }

    [Fact]
    public async Task GetPlayerAsync_keeps_unknown_metrics_rather_than_dropping_them()
    {
        // A metric this package has never heard of, in the shape a new boss would arrive in.
        const string body = """
            {
              "id": 1, "username": "x", "displayName": "X", "type": "regular", "build": "main",
              "status": "active", "patron": false, "exp": 1, "ehp": 0, "ehb": 0, "ttm": 0,
              "tt200m": 0, "registeredAt": "2020-01-01T00:00:00.000Z",
              "latestSnapshot": {
                "id": 1, "playerId": 1, "createdAt": "2026-01-01T00:00:00.000Z", "importedAt": null,
                "data": {
                  "skills": {}, "activities": {}, "computed": {},
                  "bosses": { "some_future_boss": { "metric": "some_future_boss", "kills": 7, "rank": 3, "ehb": 1.5 } }
                }
              }
            }
            """;

        using var handler = FixtureHandler.FromBody(body);

        var player = await handler.CreateWiseOldManClient().GetPlayerAsync("X", CancellationToken.None);

        // The metric families are dictionaries precisely so this survives. A record with one
        // property per known boss would drop it silently and need a release to see it.
        var boss = Assert.Single(player!.LatestSnapshot!.Data.Bosses);
        Assert.Equal("some_future_boss", boss.Key);
        Assert.Equal(7, boss.Value.Kills);
    }

    [Fact]
    public async Task GetPlayerAsync_returns_null_for_a_player_wise_old_man_does_not_track()
    {
        using var handler = FixtureHandler.FromBody(
            File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "Fixtures", "wom-error-404.json")),
            HttpStatusCode.NotFound);

        var player = await handler.CreateWiseOldManClient().GetPlayerAsync("nobody", CancellationToken.None);

        // Not an exception: "Wise Old Man has never tracked this name" is an answer, and it is
        // not the same claim as "this account does not exist" — only the hiscores settle that.
        Assert.Null(player);
    }

    [Fact]
    public async Task GetPlayerAsync_encodes_a_name_with_a_space()
    {
        using var handler = FixtureHandler.FromFixture("wom-player.json");

        await handler.CreateWiseOldManClient().GetPlayerAsync("Lynx Titan", CancellationToken.None);

        // An unencoded space produces a malformed request line, not a lenient lookup.
        Assert.Equal("/v2/players/Lynx%20Titan", handler.LastRequestUri!.AbsolutePath);
    }

    [Fact]
    public async Task GetPlayerAsync_surfaces_the_machine_readable_error_code()
    {
        using var handler = FixtureHandler.FromBody(
            """{"code":"UNEXPECTED_ERROR","message":"Invalid period: nonsense."}""",
            HttpStatusCode.BadRequest);

        var exception = await Assert.ThrowsAsync<WiseOldManApiException>(
            () => handler.CreateWiseOldManClient().GetPlayerAsync("x", CancellationToken.None));

        // The code is the stable half. The message is prose and can be reworded without notice.
        Assert.Equal("UNEXPECTED_ERROR", exception.ErrorCode);
        Assert.Equal(HttpStatusCode.BadRequest, exception.StatusCode);
        Assert.False(exception.IsTransient);
    }

    [Fact]
    public async Task GetPlayerAsync_does_not_let_a_non_json_error_body_hide_the_status()
    {
        // Cloudflare sits in front of this API and serves HTML on some failures.
        using var handler = FixtureHandler.FromBody(
            "<html>502 Bad Gateway</html>",
            HttpStatusCode.BadGateway,
            "text/html");

        var exception = await Assert.ThrowsAsync<WiseOldManApiException>(
            () => handler.CreateWiseOldManClient().GetPlayerAsync("x", CancellationToken.None));

        Assert.Equal(HttpStatusCode.BadGateway, exception.StatusCode);
        Assert.Null(exception.ErrorCode);
        Assert.True(exception.IsTransient);
    }

    [Theory]
    [InlineData(HttpStatusCode.TooManyRequests, true)]
    [InlineData(HttpStatusCode.InternalServerError, true)]
    [InlineData(HttpStatusCode.BadRequest, false)]
    [InlineData(HttpStatusCode.Forbidden, false)]
    public void IsTransient_classifies_the_statuses_a_caller_backs_off_on(HttpStatusCode status, bool expected)
    {
        // 429 is the one that matters: 20 requests per 60 seconds without a key means a sweep
        // over a group meets it. 403 does not: it means the User-Agent was rejected, and
        // retrying an unacceptable agent just spends the allowance faster.
        var exception = new WiseOldManApiException("x") { StatusCode = status };

        Assert.Equal(expected, exception.IsTransient);
    }

    [Fact]
    public void Constructor_refuses_a_client_with_no_user_agent()
    {
        using var http = new HttpClient { BaseAddress = new Uri("https://api.wiseoldman.net/v2/") };

        // Verified live: a request sent as curl/8.0 is answered with 403. Failing here beats
        // discovering it as a 403 storm in production.
        Assert.Throws<InvalidOperationException>(() => new WiseOldManClient(http));
    }

    [Fact]
    public async Task GetPlayerGainsAsync_parses_every_metric_family()
    {
        using var handler = FixtureHandler.FromFixture("wom-gained-week.json");

        var gains = await handler.CreateWiseOldManClient()
            .GetPlayerGainsAsync("Psikoi", WiseOldManPeriod.Week, CancellationToken.None);

        Assert.NotNull(gains);
        Assert.NotEmpty(gains.Data.Skills);
        Assert.NotEmpty(gains.Data.Bosses);
        Assert.NotEmpty(gains.Data.Activities);
        Assert.NotEmpty(gains.Data.Computed);

        var overall = gains.Data.Skills["overall"];
        Assert.Equal(322_896_484d, overall.Experience.End);
        Assert.Equal(799d, overall.Rank.Gained);
    }

    [Fact]
    public async Task GetPlayerGainsAsync_reports_the_window_the_server_actually_used()
    {
        using var handler = FixtureHandler.FromFixture("wom-gained-week.json");

        var gains = await handler.CreateWiseOldManClient()
            .GetPlayerGainsAsync("Psikoi", WiseOldManPeriod.Week, CancellationToken.None);

        // The window is bounded by the snapshots that exist, so period=year on an account
        // first tracked yesterday returns a day. Dividing by the period asked for would
        // overstate the rate by up to 365x.
        Assert.NotNull(gains!.StartsAt);
        Assert.NotNull(gains.EndsAt);
        Assert.True(gains.EndsAt > gains.StartsAt);
    }

    [Fact]
    public async Task GetPlayerGainsAsync_distinguishes_never_ranked_from_no_progress()
    {
        using var handler = FixtureHandler.FromFixture("wom-gained-week.json");

        var gains = await handler.CreateWiseOldManClient()
            .GetPlayerGainsAsync("Psikoi", WiseOldManPeriod.Week, CancellationToken.None);

        // An activity the player has never been ranked in reads start: -1, end: -1, gained: 0 —
        // identical to a ranked activity with no progress unless IsRanked is consulted.
        var neverRanked = gains!.Data.Activities.Values.First(activity => !activity.Score.IsRanked);

        Assert.Equal(0d, neverRanked.Score.Gained);
        Assert.Equal(-1d, neverRanked.Score.Start);
        Assert.False(neverRanked.Score.IsRanked);
    }

    [Theory]
    [InlineData(WiseOldManPeriod.FiveMinutes, "five_min")]
    [InlineData(WiseOldManPeriod.Day, "day")]
    [InlineData(WiseOldManPeriod.Week, "week")]
    [InlineData(WiseOldManPeriod.Month, "month")]
    [InlineData(WiseOldManPeriod.Year, "year")]
    public async Task GetPlayerGainsAsync_sends_the_wire_value_for_each_period(WiseOldManPeriod period, string wire)
    {
        using var handler = FixtureHandler.FromFixture("wom-gained-week.json");

        await handler.CreateWiseOldManClient().GetPlayerGainsAsync("Psikoi", period, CancellationToken.None);

        // Anything outside this set is a 400, so the mapping is the whole contract.
        Assert.Contains($"period={wire}", handler.LastRequestUri!.Query, StringComparison.Ordinal);
    }

    [Fact]
    public async Task GetPlayerSnapshotsAsync_parses_a_list_newest_first()
    {
        using var handler = FixtureHandler.FromFixture("wom-snapshots-week.json");

        var snapshots = await handler.CreateWiseOldManClient()
            .GetPlayerSnapshotsAsync("Psikoi", WiseOldManPeriod.Week, CancellationToken.None);

        Assert.NotNull(snapshots);
        Assert.Equal(2, snapshots.Count);
        Assert.True(snapshots[0].CreatedAt >= snapshots[1].CreatedAt);
        Assert.NotEmpty(snapshots[0].Data.Skills);
    }

    [Fact]
    public async Task Snapshot_activities_report_score_zero_while_rank_stays_minus_one()
    {
        using var handler = FixtureHandler.FromFixture("wom-player.json");

        var player = await handler.CreateWiseOldManClient().GetPlayerAsync("Psikoi", CancellationToken.None);

        // The snapshot route and the gains route disagree about the same underlying value:
        // score reads 0 here and -1 under /gained. Rank is the field that means the same thing
        // on both, which is why IsRanked keys off it.
        var unranked = player!.LatestSnapshot!.Data.Activities.Values.First(activity => !activity.IsRanked);

        Assert.Equal(-1, unranked.Rank);
        Assert.Equal(0, unranked.Score);
    }

    [Fact]
    public async Task GetGroupAsync_parses_the_group_and_its_roster()
    {
        using var handler = FixtureHandler.FromFixture("wom-group.json");

        var group = await handler.CreateWiseOldManClient().GetGroupAsync(139, CancellationToken.None);

        Assert.NotNull(group);
        Assert.Equal(139, group.Id);
        Assert.Equal(11, group.MemberCount);
        Assert.NotEmpty(group.Memberships);
        Assert.Equal("moderator", group.Memberships[0].Role);
        Assert.NotNull(group.Memberships[0].Player);
    }

    [Fact]
    public async Task Embedded_players_report_a_null_combat_level_rather_than_zero()
    {
        using var handler = FixtureHandler.FromFixture("wom-group.json");

        var group = await handler.CreateWiseOldManClient().GetGroupAsync(139, CancellationToken.None);

        // The group route's embedded players stop short of combatLevel. Null says "this route
        // did not supply it"; a zero would be a claim about the account, and a false one.
        Assert.Null(group!.Memberships[0].Player!.CombatLevel);
        Assert.Null(group.Memberships[0].Player!.LatestSnapshot);
    }

    [Fact]
    public async Task GetGroupGainsAsync_parses_the_leaderboard()
    {
        using var handler = FixtureHandler.FromFixture("wom-group-gained.json");

        var rows = await handler.CreateWiseOldManClient()
            .GetGroupGainsAsync(139, "overall", WiseOldManPeriod.Week, cancellationToken: CancellationToken.None);

        Assert.NotNull(rows);
        Assert.NotEmpty(rows);
        Assert.NotNull(rows[0].Player);
        Assert.True(rows[0].Data.Gained > 0);
    }

    [Fact]
    public async Task GetGroupGainsAsync_omits_paging_parameters_it_was_not_given()
    {
        using var handler = FixtureHandler.FromFixture("wom-group-gained.json");

        await handler.CreateWiseOldManClient()
            .GetGroupGainsAsync(139, "overall", WiseOldManPeriod.Week, cancellationToken: CancellationToken.None);

        var query = handler.LastRequestUri!.Query;
        Assert.Contains("metric=overall", query, StringComparison.Ordinal);
        Assert.DoesNotContain("limit=", query, StringComparison.Ordinal);
        Assert.DoesNotContain("offset=", query, StringComparison.Ordinal);
    }

    [Fact]
    public async Task GetGroupGainsAsync_sends_paging_parameters_it_was_given()
    {
        using var handler = FixtureHandler.FromFixture("wom-group-gained.json");

        await handler.CreateWiseOldManClient()
            .GetGroupGainsAsync(139, "zulrah", WiseOldManPeriod.Month, limit: 50, offset: 100, CancellationToken.None);

        var query = handler.LastRequestUri!.Query;
        Assert.Contains("metric=zulrah", query, StringComparison.Ordinal);
        Assert.Contains("period=month", query, StringComparison.Ordinal);
        Assert.Contains("limit=50", query, StringComparison.Ordinal);
        Assert.Contains("offset=100", query, StringComparison.Ordinal);
    }

    [Fact]
    public async Task GetCompetitionAsync_parses_standings_and_both_metric_fields()
    {
        using var handler = FixtureHandler.FromFixture("wom-competition.json");

        var competition = await handler.CreateWiseOldManClient().GetCompetitionAsync(153841, CancellationToken.None);

        Assert.NotNull(competition);
        Assert.Equal("classic", competition.Type);

        // Both are populated. The singular field predates multi-metric competitions; reading
        // only it scores a multi-metric competition on one of its metrics.
        Assert.Equal("sailing", competition.Metric);
        Assert.NotEmpty(competition.Metrics);
        Assert.Equal("sailing", competition.Metrics[0].Metric);

        var top = competition.Participations[0];
        Assert.NotNull(top.Player);
        Assert.NotEmpty(top.Deltas);
        Assert.Equal(top.Progress.Gained, top.Deltas[0].Values.Gained);
    }

    [Fact]
    public async Task GetCompetitionAsync_reports_the_full_entrant_count_not_the_page_it_returned()
    {
        using var handler = FixtureHandler.FromFixture("wom-competition.json");

        var competition = await handler.CreateWiseOldManClient().GetCompetitionAsync(153841, CancellationToken.None);

        // The recorded body is trimmed to three participations; participantCount is the real
        // figure and is what a caller should display.
        Assert.Equal(439, competition!.ParticipantCount);
        Assert.Equal(3, competition.Participations.Count);
    }

    [Fact]
    public async Task GetEfficiencyRatesAsync_parses_methods_and_bonuses()
    {
        using var handler = FixtureHandler.FromFixture("wom-rates.json");

        var rates = await handler.CreateWiseOldManClient()
            .GetEfficiencyRatesAsync("ehp", "main", CancellationToken.None);

        Assert.NotEmpty(rates);

        var attack = rates.First(rate => rate.Skill == "attack");
        Assert.NotEmpty(attack.Methods);
        Assert.Equal(0, attack.Methods[0].StartExp);

        // Bonuses are the by-product experience one skill grants another. Often empty, which is
        // an absence rather than a missing field.
        var defence = rates.First(rate => rate.Skill == "defence");
        Assert.NotEmpty(defence.Bonuses);
        Assert.Equal("ranged", defence.Bonuses[0].BonusSkill);
    }

    [Theory]
    [InlineData("regular", HiscoreTable.Main)]
    [InlineData("ironman", HiscoreTable.Ironman)]
    [InlineData("hardcore", HiscoreTable.HardcoreIronman)]
    [InlineData("ultimate", HiscoreTable.UltimateIronman)]
    public void AccountType_maps_a_known_type_onto_its_hiscore_table(string type, HiscoreTable expected)
    {
        // Worth having: detecting this against Jagex costs up to one request per table.
        Assert.Equal(expected, new WiseOldManPlayer { Type = type }.AccountType);
    }

    [Theory]
    [InlineData("unknown")]
    [InlineData("some_future_game_mode")]
    [InlineData("")]
    public void AccountType_returns_null_rather_than_guessing_at_an_unfamiliar_type(string type)
    {
        // Null means "ask Jagex". Defaulting to Main would attribute an ironman's timeline to
        // the wrong table, which is worse than one extra request.
        Assert.Null(new WiseOldManPlayer { Type = type }.AccountType);
    }

    [Fact]
    public void EncodeUsername_trims_before_encoding()
    {
        // A trailing space would otherwise become %20 and turn a valid name into a 404.
        Assert.Equal("Lynx%20Titan", WiseOldManClient.EncodeUsername("  Lynx Titan  "));
    }
}
