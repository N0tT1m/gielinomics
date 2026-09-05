using Gielinomics.Data;
using Xunit;

namespace Gielinomics.Data.Tests;

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
