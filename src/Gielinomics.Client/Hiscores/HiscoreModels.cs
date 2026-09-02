using System.Text.Json.Serialization;

namespace Gielinomics.Client.Hiscores;

/// <summary>
/// One skill's standing.
/// </summary>
/// <remarks>
/// <c>-1</c> means unranked and is preserved rather than normalised. An unranked skill is not
/// a rank of zero, and a level of -1 is not level 1.
/// </remarks>
public sealed record HiscoreSkill
{
    /// <summary>Positional index. Stable for existing skills; new skills are appended.</summary>
    [JsonPropertyName("id")]
    public int Id { get; init; }

    /// <summary>Skill name as the API reports it.</summary>
    [JsonPropertyName("name")]
    public string Name { get; init; } = string.Empty;

    /// <summary>Rank, or -1 when unranked.</summary>
    [JsonPropertyName("rank")]
    public int Rank { get; init; }

    /// <summary>Level, or -1 when unranked.</summary>
    [JsonPropertyName("level")]
    public int Level { get; init; }

    /// <summary>Experience, or -1 when unranked. Overall exceeds <see cref="int"/> on maxed accounts.</summary>
    [JsonPropertyName("xp")]
    public long Xp { get; init; }

    /// <summary>Whether this skill is ranked at all.</summary>
    /// <remarks>Not serialised: the payload is hashed for dedup, so it carries only what the wire carried.</remarks>
    [JsonIgnore]
    public bool IsRanked => Rank >= 0;
}

/// <summary>One activity, boss or clue tier's standing.</summary>
public sealed record HiscoreActivity
{
    /// <summary>Positional index within the activity block.</summary>
    [JsonPropertyName("id")]
    public int Id { get; init; }

    /// <summary>Activity name as the API reports it.</summary>
    [JsonPropertyName("name")]
    public string Name { get; init; } = string.Empty;

    /// <summary>Rank, or -1 when unranked.</summary>
    [JsonPropertyName("rank")]
    public int Rank { get; init; }

    /// <summary>Score or kill count, or -1 when unranked.</summary>
    [JsonPropertyName("score")]
    public long Score { get; init; }

    /// <summary>Whether this activity is ranked at all.</summary>
    /// <remarks>Not serialised, for the same reason as <see cref="HiscoreSkill.IsRanked"/>.</remarks>
    [JsonIgnore]
    public bool IsRanked => Rank >= 0;
}

/// <summary>
/// A player's full hiscore standing on one table.
/// </summary>
/// <remarks>
/// From <c>index_lite.json</c>, which — verified live — returns <c>id</c> <b>and</b> <c>name</c>
/// per entry. That removes the positional-CSV fragility this design was originally built
/// around: the ordering is no longer the entire contract, because the server names each field.
/// <see cref="HiscoreCsvParser"/> remains for the <c>index_lite.ws</c> fallback, where it is.
/// </remarks>
public sealed record HiscoreProfile
{
    /// <summary>The display name the API echoed back.</summary>
    [JsonPropertyName("name")]
    public string Name { get; init; } = string.Empty;

    /// <summary>Skills, in the server's order.</summary>
    [JsonPropertyName("skills")]
    public IReadOnlyList<HiscoreSkill> Skills { get; init; } = [];

    /// <summary>Activities, bosses and clue tiers, in the server's order.</summary>
    [JsonPropertyName("activities")]
    public IReadOnlyList<HiscoreActivity> Activities { get; init; } = [];

    /// <summary>Which table this standing came from. Set by the client, not the wire.</summary>
    [JsonIgnore]
    public HiscoreTable Table { get; init; }
}
