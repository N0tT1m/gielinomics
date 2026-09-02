using Gielinomics.Client.Hiscores;
using Gielinomics.Data;
using Xunit;

namespace Gielinomics.Client.Tests;

/// <summary>
/// The positional CSV fallback, where the line ordering genuinely is the entire contract.
/// </summary>
public class HiscoreCsvParserTests
{
    private static string LoadFixture(string name)
        => File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "Fixtures", name));

    [Fact]
    public void Parse_names_each_positional_line_from_the_mapping()
    {
        var profile = HiscoreCsvParser.Parse(LoadFixture("hiscore-main.csv"), HiscoreMapping.Current, out _);

        Assert.Equal(25, profile.Skills.Count);
        Assert.Equal("Overall", profile.Skills[0].Name);
        Assert.Equal("Attack", profile.Skills[1].Name);
        Assert.Equal("Sailing", profile.Skills[24].Name);

        Assert.Equal(2278, profile.Skills[0].Level);
        Assert.Equal(4_600_000_000L, profile.Skills[0].Xp);
    }

    [Fact]
    public void Parse_reads_the_activity_block_after_the_skills()
    {
        var profile = HiscoreCsvParser.Parse(LoadFixture("hiscore-main.csv"), HiscoreMapping.Current, out _);

        // The recorded response carries 116 lines: 25 skills then 91 activities.
        Assert.Equal(91, profile.Activities.Count);
        Assert.Equal("Grid Points", profile.Activities[0].Name);
        Assert.Equal(0, profile.Activities[0].Id);
        Assert.Equal("Zulrah", profile.Activities[^1].Name);
    }

    [Fact]
    public void Parse_counts_the_overflow_against_the_expected_line_count()
    {
        var mapping = new HiscoreMapping
        {
            Version = 99,
            SkillNames = ["Overall"],
            ActivityNames = ["Something"],
        };

        HiscoreCsvParser.Parse("1,2,3\n4,5\n6,7\n8,9\n", mapping, out var unknown);

        Assert.Equal(2, mapping.ExpectedLineCount);
        Assert.Equal(2, unknown);
    }

    [Fact]
    public void Parse_preserves_unranked_as_minus_one()
    {
        var mapping = new HiscoreMapping
        {
            Version = 99,
            SkillNames = ["Overall"],
            ActivityNames = ["Something"],
        };

        var profile = HiscoreCsvParser.Parse("-1,-1,-1\n-1,-1\n", mapping, out _);

        Assert.Equal(-1, profile.Skills[0].Rank);
        Assert.Equal(-1, profile.Activities[0].Score);
        Assert.False(profile.Skills[0].IsRanked);
    }

    [Fact]
    public void Parse_rejects_a_line_that_is_not_in_the_documented_shape()
    {
        var mapping = new HiscoreMapping { Version = 99, SkillNames = ["Overall"], ActivityNames = [] };

        Assert.Throws<FormatException>(() => HiscoreCsvParser.Parse("1,2\n", mapping, out _));
        Assert.Throws<FormatException>(() => HiscoreCsvParser.Parse("a,b,c\n", mapping, out _));
    }

    [Fact]
    public void Parse_tolerates_a_response_shorter_than_the_mapping()
    {
        // A truncated response should yield fewer entries, not an index-out-of-range.
        var profile = HiscoreCsvParser.Parse("1,2,3\n", HiscoreMapping.Current, out var unknown);

        Assert.Single(profile.Skills);
        Assert.Empty(profile.Activities);
        Assert.Equal(0, unknown);
    }

    [Fact]
    public void Current_mapping_matches_the_live_response_shape()
    {
        // Verified live on 2 September 2026: 25 skills then 91 activities, 116 CSV lines.
        Assert.Equal(25, HiscoreMapping.Current.SkillNames.Count);
        Assert.Equal("Sailing", HiscoreMapping.Current.SkillNames[^1]);
        Assert.Equal(91, HiscoreMapping.Current.ActivityNames.Count);
        Assert.Equal(116, HiscoreMapping.Current.ExpectedLineCount);
        Assert.Equal(1, HiscoreMapping.Current.Version);

        var fixtureLines = LoadFixture("hiscore-main.csv").Split('\n', StringSplitOptions.RemoveEmptyEntries);
        Assert.Equal(HiscoreMapping.Current.ExpectedLineCount, fixtureLines.Length);
    }
}

/// <summary>
/// Name normalisation. Names are case-insensitive, allow spaces, and change over time; the
/// stable internal ID is what a timeline hangs off.
/// </summary>
public class PlayerNameNormalisationTests
{
    [Theory]
    [InlineData("Lynx Titan", "lynx titan")]
    [InlineData("LYNX TITAN", "lynx titan")]
    [InlineData("Lynx_Titan", "lynx titan")]
    [InlineData("  Lynx   Titan  ", "lynx titan")]
    [InlineData("lynx titan", "lynx titan")]
    [InlineData("Zezima", "zezima")]
    public void Normalise_collapses_the_forms_that_mean_the_same_account(string input, string expected)
        => Assert.Equal(expected, PlayerRepository.Normalise(input));

    [Fact]
    public void Normalise_treats_a_non_breaking_space_as_a_space()
    {
        // Tools and the game use these interchangeably; a user pasting one must still resolve.
        Assert.Equal("lynx titan", PlayerRepository.Normalise("Lynx\u00A0Titan"));
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("   ")]
    public void Normalise_rejects_a_name_with_nothing_in_it(string? input)
        => Assert.ThrowsAny<ArgumentException>(() => PlayerRepository.Normalise(input!));
}
