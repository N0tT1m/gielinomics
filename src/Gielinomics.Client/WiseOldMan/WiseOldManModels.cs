using System.Text.Json.Serialization;
using Gielinomics.Client.Hiscores;

namespace Gielinomics.Client.WiseOldMan;

/// <summary>
/// A change in one metric over a window.
/// </summary>
/// <remarks>
/// <para>
/// One type for every delta on every route, deliberately. The same three fields carry
/// experience on one metric, kill count on another and EHP on a third, because the metric is
/// chosen by the caller at request time. A <see cref="long"/> would truncate the fractional
/// ones; <see cref="double"/> represents every value in range — ranks, levels, kills, and an
/// overall experience ceiling of 4.8 billion — exactly, being well under 2^53.
/// </para>
/// <para>
/// <b>A <see cref="Gained"/> of zero does not mean "no progress".</b> An unranked metric
/// reports <c>-1</c> for both <see cref="Start"/> and <see cref="End"/>, and the API computes
/// the difference anyway, so "never ranked" and "ranked but idle" are the same zero. Check
/// <see cref="IsRanked"/> before reading a zero as a fact about the player.
/// </para>
/// </remarks>
public sealed record MetricDelta
{
    /// <summary>The difference over the window. See the type remarks before trusting a zero.</summary>
    [JsonPropertyName("gained")]
    public double Gained { get; init; }

    /// <summary>Value at the start of the window, or -1 when unranked then.</summary>
    [JsonPropertyName("start")]
    public double Start { get; init; }

    /// <summary>Value at the end of the window, or -1 when unranked then.</summary>
    [JsonPropertyName("end")]
    public double End { get; init; }

    /// <summary>Whether the player was ranked at both ends of the window.</summary>
    [JsonIgnore]
    public bool IsRanked => Start >= 0 && End >= 0;
}

/// <summary>One skill's standing in a snapshot.</summary>
public sealed record WiseOldManSkill
{
    /// <summary>Metric name, e.g. <c>overall</c> or <c>woodcutting</c>.</summary>
    [JsonPropertyName("metric")]
    public string Metric { get; init; } = string.Empty;

    /// <summary>Experience, or -1 when unranked.</summary>
    [JsonPropertyName("experience")]
    public long Experience { get; init; }

    /// <summary>Rank, or -1 when unranked.</summary>
    [JsonPropertyName("rank")]
    public int Rank { get; init; }

    /// <summary>Level, or -1 when unranked.</summary>
    [JsonPropertyName("level")]
    public int Level { get; init; }

    /// <summary>Efficient hours played attributed to this skill.</summary>
    [JsonPropertyName("ehp")]
    public double Ehp { get; init; }

    /// <summary>Whether this skill is ranked at all.</summary>
    [JsonIgnore]
    public bool IsRanked => Rank >= 0;
}

/// <summary>One boss's standing in a snapshot.</summary>
public sealed record WiseOldManBoss
{
    /// <summary>Metric name, e.g. <c>abyssal_sire</c>.</summary>
    [JsonPropertyName("metric")]
    public string Metric { get; init; } = string.Empty;

    /// <summary>Kill count, or -1 when unranked.</summary>
    [JsonPropertyName("kills")]
    public long Kills { get; init; }

    /// <summary>Rank, or -1 when unranked.</summary>
    [JsonPropertyName("rank")]
    public int Rank { get; init; }

    /// <summary>Efficient hours bossed attributed to this boss.</summary>
    [JsonPropertyName("ehb")]
    public double Ehb { get; init; }

    /// <summary>Whether this boss is ranked at all.</summary>
    [JsonIgnore]
    public bool IsRanked => Rank >= 0;
}

/// <summary>
/// One activity, clue tier or minigame's standing in a snapshot.
/// </summary>
/// <remarks>
/// Unlike the hiscores — and unlike this route's own gains equivalent — an unranked activity
/// reports <see cref="Score"/> as <c>0</c> while <see cref="Rank"/> stays <c>-1</c>. Verified
/// live: the same activity reads <c>score: 0</c> here and <c>start: -1, end: -1</c> under
/// <c>/gained</c>. <see cref="Rank"/> is the field to test.
/// </remarks>
public sealed record WiseOldManActivity
{
    /// <summary>Metric name, e.g. <c>clue_scrolls_all</c>.</summary>
    [JsonPropertyName("metric")]
    public string Metric { get; init; } = string.Empty;

    /// <summary>Score or completion count. Zero when unranked — see the type remarks.</summary>
    [JsonPropertyName("score")]
    public long Score { get; init; }

    /// <summary>Rank, or -1 when unranked.</summary>
    [JsonPropertyName("rank")]
    public int Rank { get; init; }

    /// <summary>Whether this activity is ranked at all.</summary>
    [JsonIgnore]
    public bool IsRanked => Rank >= 0;
}

/// <summary>A derived metric — EHP or EHB — and the player's rank in it.</summary>
public sealed record WiseOldManComputed
{
    /// <summary>Metric name, <c>ehp</c> or <c>ehb</c>.</summary>
    [JsonPropertyName("metric")]
    public string Metric { get; init; } = string.Empty;

    /// <summary>The computed value.</summary>
    [JsonPropertyName("value")]
    public double Value { get; init; }

    /// <summary>Rank in this metric, or -1 when unranked.</summary>
    [JsonPropertyName("rank")]
    public int Rank { get; init; }
}

/// <summary>
/// The four metric families in a snapshot.
/// </summary>
/// <remarks>
/// Keyed by metric name rather than modelled as one property per skill and boss. The wire
/// format is a JSON object keyed by metric, and the set grows — a league, a new boss, a new
/// skill. A dictionary carries an unrecognised metric through to the caller; a record with 71
/// named boss properties silently drops the seventy-second and needs a release to see it.
/// </remarks>
public sealed record WiseOldManSnapshotData
{
    /// <summary>Skills, keyed by metric name. Includes the <c>overall</c> pseudo-skill.</summary>
    [JsonPropertyName("skills")]
    public IReadOnlyDictionary<string, WiseOldManSkill> Skills { get; init; } =
        new Dictionary<string, WiseOldManSkill>();

    /// <summary>Bosses, keyed by metric name.</summary>
    [JsonPropertyName("bosses")]
    public IReadOnlyDictionary<string, WiseOldManBoss> Bosses { get; init; } =
        new Dictionary<string, WiseOldManBoss>();

    /// <summary>Activities, clue tiers and minigames, keyed by metric name.</summary>
    [JsonPropertyName("activities")]
    public IReadOnlyDictionary<string, WiseOldManActivity> Activities { get; init; } =
        new Dictionary<string, WiseOldManActivity>();

    /// <summary>Derived metrics, keyed by metric name.</summary>
    [JsonPropertyName("computed")]
    public IReadOnlyDictionary<string, WiseOldManComputed> Computed { get; init; } =
        new Dictionary<string, WiseOldManComputed>();
}

/// <summary>A player's full standing at one point in time.</summary>
public sealed record WiseOldManSnapshot
{
    /// <summary>Snapshot identifier.</summary>
    [JsonPropertyName("id")]
    public long Id { get; init; }

    /// <summary>The player this snapshot belongs to.</summary>
    [JsonPropertyName("playerId")]
    public long PlayerId { get; init; }

    /// <summary>When the snapshot was taken.</summary>
    [JsonPropertyName("createdAt")]
    public DateTimeOffset CreatedAt { get; init; }

    /// <summary>When it was imported from an external source, or null if it was polled directly.</summary>
    [JsonPropertyName("importedAt")]
    public DateTimeOffset? ImportedAt { get; init; }

    /// <summary>The standing itself.</summary>
    [JsonPropertyName("data")]
    public WiseOldManSnapshotData Data { get; init; } = new();
}

/// <summary>
/// A tracked account.
/// </summary>
/// <remarks>
/// Returned in two depths. <c>/players/{username}</c> gives every field including
/// <see cref="LatestSnapshot"/>; the copies embedded in group memberships and competition
/// participations stop at <see cref="LastImportedAt"/>. That is why
/// <see cref="CombatLevel"/> is nullable — absent means "this route did not supply it", which
/// is not the same claim as combat level zero.
/// </remarks>
public sealed record WiseOldManPlayer
{
    /// <summary>Wise Old Man's own identifier, stable across renames.</summary>
    [JsonPropertyName("id")]
    public long Id { get; init; }

    /// <summary>Normalised name: lower-cased, spaces preserved. The lookup key.</summary>
    [JsonPropertyName("username")]
    public string Username { get; init; } = string.Empty;

    /// <summary>Name as displayed in game, with its original casing.</summary>
    [JsonPropertyName("displayName")]
    public string DisplayName { get; init; } = string.Empty;

    /// <summary>
    /// Account type: <c>regular</c>, <c>ironman</c>, <c>hardcore</c>, <c>ultimate</c>, <c>unknown</c>.
    /// </summary>
    /// <remarks>
    /// A string, not an enum. A new game mode ships as a new value here, and a
    /// <c>JsonStringEnumConverter</c> would turn that into an exception on every player in the
    /// group rather than one unfamiliar label. Use <see cref="AccountType"/> to map it.
    /// </remarks>
    [JsonPropertyName("type")]
    public string Type { get; init; } = string.Empty;

    /// <summary>Account build: <c>main</c>, <c>f2p</c>, <c>lvl3</c>, <c>zerker</c> and so on. A string, for the same reason as <see cref="Type"/>.</summary>
    [JsonPropertyName("build")]
    public string Build { get; init; } = string.Empty;

    /// <summary>Tracking status: <c>active</c>, <c>unranked</c>, <c>flagged</c>, <c>archived</c>, <c>banned</c>.</summary>
    [JsonPropertyName("status")]
    public string Status { get; init; } = string.Empty;

    /// <summary>Two-letter country code the player self-reported, or null.</summary>
    [JsonPropertyName("country")]
    public string? Country { get; init; }

    /// <summary>Whether the account holder supports Wise Old Man financially.</summary>
    [JsonPropertyName("patron")]
    public bool Patron { get; init; }

    /// <summary>Total experience across all skills.</summary>
    [JsonPropertyName("exp")]
    public long Exp { get; init; }

    /// <summary>Efficient hours played.</summary>
    [JsonPropertyName("ehp")]
    public double Ehp { get; init; }

    /// <summary>Efficient hours bossed.</summary>
    [JsonPropertyName("ehb")]
    public double Ehb { get; init; }

    /// <summary>Time to max, in efficient hours.</summary>
    [JsonPropertyName("ttm")]
    public double Ttm { get; init; }

    /// <summary>Time to 200m all, in efficient hours.</summary>
    [JsonPropertyName("tt200m")]
    public double Tt200m { get; init; }

    /// <summary>When Wise Old Man first saw this account.</summary>
    [JsonPropertyName("registeredAt")]
    public DateTimeOffset RegisteredAt { get; init; }

    /// <summary>When it was last polled.</summary>
    [JsonPropertyName("updatedAt")]
    public DateTimeOffset? UpdatedAt { get; init; }

    /// <summary>When a poll last found something different. Null if nothing ever has.</summary>
    [JsonPropertyName("lastChangedAt")]
    public DateTimeOffset? LastChangedAt { get; init; }

    /// <summary>When history was last imported from an external source.</summary>
    [JsonPropertyName("lastImportedAt")]
    public DateTimeOffset? LastImportedAt { get; init; }

    /// <summary>Combat level, or null when the route did not supply it — see the type remarks.</summary>
    [JsonPropertyName("combatLevel")]
    public int? CombatLevel { get; init; }

    /// <summary>Archive record when the name was reassigned to a different account, else null.</summary>
    [JsonPropertyName("archive")]
    public WiseOldManArchive? Archive { get; init; }

    /// <summary>Moderator annotations, e.g. an opt-out or a fake-name flag. Usually empty.</summary>
    [JsonPropertyName("annotations")]
    public IReadOnlyList<WiseOldManAnnotation> Annotations { get; init; } = [];

    /// <summary>The most recent snapshot, on the single-player route only. Null elsewhere.</summary>
    [JsonPropertyName("latestSnapshot")]
    public WiseOldManSnapshot? LatestSnapshot { get; init; }

    /// <summary>
    /// <see cref="Type"/> mapped onto the hiscore table it corresponds to, or null for a value
    /// this package does not recognise.
    /// </summary>
    /// <remarks>
    /// Worth having because <see cref="IHiscoresClient.DetectAccountTypeAsync"/> costs up to
    /// one request per table against Jagex, who publish no rate limit. If Wise Old Man already
    /// knows the account, this answers the same question for free. Null means "ask Jagex", not
    /// "main": treat an unrecognised type as unknown rather than defaulting it, since the cost
    /// of guessing wrong is a timeline attributed to the wrong table.
    /// </remarks>
    [JsonIgnore]
    public HiscoreTable? AccountType => Type switch
    {
        "regular" => HiscoreTable.Main,
        "ironman" => HiscoreTable.Ironman,
        "hardcore" => HiscoreTable.HardcoreIronman,
        "ultimate" => HiscoreTable.UltimateIronman,
        _ => null,
    };
}

/// <summary>The previous holder of a display name, when the name has been reassigned.</summary>
public sealed record WiseOldManArchive
{
    /// <summary>The archived player's identifier.</summary>
    [JsonPropertyName("playerId")]
    public long PlayerId { get; init; }

    /// <summary>The name at the time of archiving.</summary>
    [JsonPropertyName("previousUsername")]
    public string? PreviousUsername { get; init; }

    /// <summary>When the account was archived.</summary>
    [JsonPropertyName("createdAt")]
    public DateTimeOffset? CreatedAt { get; init; }

    /// <summary>When the name became available to another account.</summary>
    [JsonPropertyName("restoredAt")]
    public DateTimeOffset? RestoredAt { get; init; }
}

/// <summary>A moderator note attached to an account.</summary>
public sealed record WiseOldManAnnotation
{
    /// <summary>The annotation type, e.g. <c>OPT_OUT</c>.</summary>
    [JsonPropertyName("type")]
    public string Type { get; init; } = string.Empty;

    /// <summary>When it was applied.</summary>
    [JsonPropertyName("createdAt")]
    public DateTimeOffset? CreatedAt { get; init; }
}

/// <summary>One skill's change over a window.</summary>
public sealed record WiseOldManSkillGains
{
    /// <summary>Metric name.</summary>
    [JsonPropertyName("metric")]
    public string Metric { get; init; } = string.Empty;

    /// <summary>Experience gained, and the values at each end of the window.</summary>
    [JsonPropertyName("experience")]
    public MetricDelta Experience { get; init; } = new();

    /// <summary>Efficient hours played gained.</summary>
    [JsonPropertyName("ehp")]
    public MetricDelta Ehp { get; init; } = new();

    /// <summary>Rank movement. Positive means the rank number went up, which is <i>worse</i>.</summary>
    [JsonPropertyName("rank")]
    public MetricDelta Rank { get; init; } = new();

    /// <summary>Levels gained.</summary>
    [JsonPropertyName("level")]
    public MetricDelta Level { get; init; } = new();
}

/// <summary>One boss's change over a window.</summary>
public sealed record WiseOldManBossGains
{
    /// <summary>Metric name.</summary>
    [JsonPropertyName("metric")]
    public string Metric { get; init; } = string.Empty;

    /// <summary>Kills gained.</summary>
    [JsonPropertyName("kills")]
    public MetricDelta Kills { get; init; } = new();

    /// <summary>Efficient hours bossed gained.</summary>
    [JsonPropertyName("ehb")]
    public MetricDelta Ehb { get; init; } = new();

    /// <summary>Rank movement. Positive means the rank number went up, which is <i>worse</i>.</summary>
    [JsonPropertyName("rank")]
    public MetricDelta Rank { get; init; } = new();
}

/// <summary>
/// One activity's change over a window.
/// </summary>
/// <remarks>
/// The one place the <c>-1</c> sentinel is most likely to be misread. An activity the player
/// has never been ranked in reports <c>start: -1, end: -1, gained: 0</c> — verified live — so
/// it is indistinguishable from a ranked activity with no progress unless you check
/// <see cref="MetricDelta.IsRanked"/>.
/// </remarks>
public sealed record WiseOldManActivityGains
{
    /// <summary>Metric name.</summary>
    [JsonPropertyName("metric")]
    public string Metric { get; init; } = string.Empty;

    /// <summary>Score gained. See the type remarks before reading a zero.</summary>
    [JsonPropertyName("score")]
    public MetricDelta Score { get; init; } = new();

    /// <summary>Rank movement.</summary>
    [JsonPropertyName("rank")]
    public MetricDelta Rank { get; init; } = new();
}

/// <summary>A derived metric's change over a window.</summary>
public sealed record WiseOldManComputedGains
{
    /// <summary>Metric name, <c>ehp</c> or <c>ehb</c>.</summary>
    [JsonPropertyName("metric")]
    public string Metric { get; init; } = string.Empty;

    /// <summary>The change in the computed value.</summary>
    [JsonPropertyName("value")]
    public MetricDelta Value { get; init; } = new();

    /// <summary>Rank movement.</summary>
    [JsonPropertyName("rank")]
    public MetricDelta Rank { get; init; } = new();
}

/// <summary>The four metric families, as deltas. Keyed by metric name, for the reason in <see cref="WiseOldManSnapshotData"/>.</summary>
public sealed record WiseOldManGainsData
{
    /// <summary>Skill gains, keyed by metric name.</summary>
    [JsonPropertyName("skills")]
    public IReadOnlyDictionary<string, WiseOldManSkillGains> Skills { get; init; } =
        new Dictionary<string, WiseOldManSkillGains>();

    /// <summary>Boss gains, keyed by metric name.</summary>
    [JsonPropertyName("bosses")]
    public IReadOnlyDictionary<string, WiseOldManBossGains> Bosses { get; init; } =
        new Dictionary<string, WiseOldManBossGains>();

    /// <summary>Activity gains, keyed by metric name.</summary>
    [JsonPropertyName("activities")]
    public IReadOnlyDictionary<string, WiseOldManActivityGains> Activities { get; init; } =
        new Dictionary<string, WiseOldManActivityGains>();

    /// <summary>Computed metric gains, keyed by metric name.</summary>
    [JsonPropertyName("computed")]
    public IReadOnlyDictionary<string, WiseOldManComputedGains> Computed { get; init; } =
        new Dictionary<string, WiseOldManComputedGains>();
}

/// <summary>
/// What a player gained over a window.
/// </summary>
/// <remarks>
/// The window the server actually used is echoed back in <see cref="StartsAt"/> and
/// <see cref="EndsAt"/>, and it is not necessarily the one implied by the period: it is bounded
/// by the snapshots that exist. An account first tracked yesterday returns a one-day window for
/// <c>period=year</c>. Attributing the result to a full year would overstate the rate by 365x,
/// so read these fields rather than assuming.
/// </remarks>
public sealed record WiseOldManGains
{
    /// <summary>Start of the window the server used.</summary>
    [JsonPropertyName("startsAt")]
    public DateTimeOffset? StartsAt { get; init; }

    /// <summary>End of the window the server used.</summary>
    [JsonPropertyName("endsAt")]
    public DateTimeOffset? EndsAt { get; init; }

    /// <summary>The gains themselves.</summary>
    [JsonPropertyName("data")]
    public WiseOldManGainsData Data { get; init; } = new();
}

/// <summary>Off-site links a group has advertised. Every field is nullable and usually is null.</summary>
public sealed record WiseOldManSocialLinks
{
    /// <summary>The group's website.</summary>
    [JsonPropertyName("website")]
    public string? Website { get; init; }

    /// <summary>Discord invite.</summary>
    [JsonPropertyName("discord")]
    public string? Discord { get; init; }

    /// <summary>Twitter or X profile.</summary>
    [JsonPropertyName("twitter")]
    public string? Twitter { get; init; }

    /// <summary>YouTube channel.</summary>
    [JsonPropertyName("youtube")]
    public string? Youtube { get; init; }

    /// <summary>Twitch channel.</summary>
    [JsonPropertyName("twitch")]
    public string? Twitch { get; init; }
}

/// <summary>One player's membership of a group.</summary>
public sealed record WiseOldManMembership
{
    /// <summary>The member.</summary>
    [JsonPropertyName("playerId")]
    public long PlayerId { get; init; }

    /// <summary>The group.</summary>
    [JsonPropertyName("groupId")]
    public long GroupId { get; init; }

    /// <summary>Role within the group, e.g. <c>owner</c>, <c>moderator</c>, <c>member</c>, or null.</summary>
    [JsonPropertyName("role")]
    public string? Role { get; init; }

    /// <summary>When the player joined.</summary>
    [JsonPropertyName("createdAt")]
    public DateTimeOffset? CreatedAt { get; init; }

    /// <summary>When the membership last changed.</summary>
    [JsonPropertyName("updatedAt")]
    public DateTimeOffset? UpdatedAt { get; init; }

    /// <summary>The player, without a snapshot or combat level — see <see cref="WiseOldManPlayer"/>.</summary>
    [JsonPropertyName("player")]
    public WiseOldManPlayer? Player { get; init; }
}

/// <summary>A clan or group, with its roster.</summary>
public sealed record WiseOldManGroup
{
    /// <summary>Group identifier.</summary>
    [JsonPropertyName("id")]
    public long Id { get; init; }

    /// <summary>Group name.</summary>
    [JsonPropertyName("name")]
    public string Name { get; init; } = string.Empty;

    /// <summary>In-game clan chat, or null.</summary>
    [JsonPropertyName("clanChat")]
    public string? ClanChat { get; init; }

    /// <summary>Free-text description, or null.</summary>
    [JsonPropertyName("description")]
    public string? Description { get; init; }

    /// <summary>Home world, or null.</summary>
    [JsonPropertyName("homeworld")]
    public int? Homeworld { get; init; }

    /// <summary>Whether Wise Old Man has verified the group's ownership.</summary>
    [JsonPropertyName("verified")]
    public bool Verified { get; init; }

    /// <summary>Whether the group is patron-supported.</summary>
    [JsonPropertyName("patron")]
    public bool Patron { get; init; }

    /// <summary>Whether the group is listed publicly.</summary>
    [JsonPropertyName("visible")]
    public bool Visible { get; init; }

    /// <summary>Profile image URL, or null.</summary>
    [JsonPropertyName("profileImage")]
    public string? ProfileImage { get; init; }

    /// <summary>Banner image URL, or null.</summary>
    [JsonPropertyName("bannerImage")]
    public string? BannerImage { get; init; }

    /// <summary>Wise Old Man's own ranking score for the group.</summary>
    [JsonPropertyName("score")]
    public int Score { get; init; }

    /// <summary>When the group was created.</summary>
    [JsonPropertyName("createdAt")]
    public DateTimeOffset? CreatedAt { get; init; }

    /// <summary>When it last changed.</summary>
    [JsonPropertyName("updatedAt")]
    public DateTimeOffset? UpdatedAt { get; init; }

    /// <summary>Roster size. Authoritative even when <see cref="Memberships"/> is empty, which it is on list routes.</summary>
    [JsonPropertyName("memberCount")]
    public int MemberCount { get; init; }

    /// <summary>Advertised links.</summary>
    [JsonPropertyName("socialLinks")]
    public WiseOldManSocialLinks? SocialLinks { get; init; }

    /// <summary>The roster. Populated on the single-group route only.</summary>
    [JsonPropertyName("memberships")]
    public IReadOnlyList<WiseOldManMembership> Memberships { get; init; } = [];
}

/// <summary>One member's gains in a group-wide leaderboard.</summary>
public sealed record WiseOldManGroupGains
{
    /// <summary>The member.</summary>
    [JsonPropertyName("player")]
    public WiseOldManPlayer? Player { get; init; }

    /// <summary>Start of the window the server used.</summary>
    [JsonPropertyName("startDate")]
    public DateTimeOffset? StartDate { get; init; }

    /// <summary>End of the window the server used.</summary>
    [JsonPropertyName("endDate")]
    public DateTimeOffset? EndDate { get; init; }

    /// <summary>The change in the requested metric.</summary>
    [JsonPropertyName("data")]
    public MetricDelta Data { get; init; } = new();
}

/// <summary>One metric in a multi-metric competition, with its scoring weight.</summary>
public sealed record WiseOldManCompetitionMetric
{
    /// <summary>The metric being tracked.</summary>
    [JsonPropertyName("metric")]
    public string Metric { get; init; } = string.Empty;

    /// <summary>Its weight in the combined score.</summary>
    [JsonPropertyName("weight")]
    public double Weight { get; init; }

    /// <summary>When it was added to the competition.</summary>
    [JsonPropertyName("createdAt")]
    public DateTimeOffset? CreatedAt { get; init; }
}

/// <summary>A participant's progress in one of a competition's metrics.</summary>
public sealed record WiseOldManParticipationDelta
{
    /// <summary>The metric.</summary>
    [JsonPropertyName("metric")]
    public string Metric { get; init; } = string.Empty;

    /// <summary>Progress in the metric's own unit — experience, kills or score.</summary>
    [JsonPropertyName("values")]
    public MetricDelta Values { get; init; } = new();

    /// <summary>Levels gained, for skill metrics.</summary>
    [JsonPropertyName("levels")]
    public MetricDelta Levels { get; init; } = new();
}

/// <summary>One player's entry in a competition.</summary>
public sealed record WiseOldManParticipation
{
    /// <summary>The participant.</summary>
    [JsonPropertyName("playerId")]
    public long PlayerId { get; init; }

    /// <summary>The competition.</summary>
    [JsonPropertyName("competitionId")]
    public long CompetitionId { get; init; }

    /// <summary>Team name in a team competition, else null.</summary>
    [JsonPropertyName("teamName")]
    public string? TeamName { get; init; }

    /// <summary>When the player was entered.</summary>
    [JsonPropertyName("createdAt")]
    public DateTimeOffset? CreatedAt { get; init; }

    /// <summary>When their progress last changed.</summary>
    [JsonPropertyName("updatedAt")]
    public DateTimeOffset? UpdatedAt { get; init; }

    /// <summary>The participant, without a snapshot or combat level.</summary>
    [JsonPropertyName("player")]
    public WiseOldManPlayer? Player { get; init; }

    /// <summary>Per-metric progress. Has one entry per competition metric.</summary>
    [JsonPropertyName("deltas")]
    public IReadOnlyList<WiseOldManParticipationDelta> Deltas { get; init; } = [];

    /// <summary>Progress in the competition's primary metric. Mirrors the matching entry in <see cref="Deltas"/>.</summary>
    [JsonPropertyName("progress")]
    public MetricDelta Progress { get; init; } = new();

    /// <summary>Levels gained in the primary metric.</summary>
    [JsonPropertyName("levels")]
    public MetricDelta Levels { get; init; } = new();
}

/// <summary>
/// A competition and its standings.
/// </summary>
/// <remarks>
/// <see cref="Metric"/> and <see cref="Metrics"/> both exist and both are populated. The
/// singular field predates multi-metric competitions and carries the primary metric; the plural
/// carries every metric with its weight. Reading only the singular one silently scores a
/// multi-metric competition on one of its metrics.
/// </remarks>
public sealed record WiseOldManCompetition
{
    /// <summary>Competition identifier.</summary>
    [JsonPropertyName("id")]
    public long Id { get; init; }

    /// <summary>Competition title.</summary>
    [JsonPropertyName("title")]
    public string Title { get; init; } = string.Empty;

    /// <summary>Format: <c>classic</c> or <c>team</c>.</summary>
    [JsonPropertyName("type")]
    public string Type { get; init; } = string.Empty;

    /// <summary>The primary metric. See the type remarks.</summary>
    [JsonPropertyName("metric")]
    public string Metric { get; init; } = string.Empty;

    /// <summary>Every metric being scored, with weights.</summary>
    [JsonPropertyName("metrics")]
    public IReadOnlyList<WiseOldManCompetitionMetric> Metrics { get; init; } = [];

    /// <summary>When it starts.</summary>
    [JsonPropertyName("startsAt")]
    public DateTimeOffset StartsAt { get; init; }

    /// <summary>When it ends.</summary>
    [JsonPropertyName("endsAt")]
    public DateTimeOffset EndsAt { get; init; }

    /// <summary>The hosting group, or null for an open competition.</summary>
    [JsonPropertyName("groupId")]
    public long? GroupId { get; init; }

    /// <summary>Wise Old Man's own ranking score for the competition.</summary>
    [JsonPropertyName("score")]
    public int Score { get; init; }

    /// <summary>Whether the competition is listed publicly.</summary>
    [JsonPropertyName("visible")]
    public bool Visible { get; init; }

    /// <summary>When it was created.</summary>
    [JsonPropertyName("createdAt")]
    public DateTimeOffset? CreatedAt { get; init; }

    /// <summary>When it last changed.</summary>
    [JsonPropertyName("updatedAt")]
    public DateTimeOffset? UpdatedAt { get; init; }

    /// <summary>Entrant count. Authoritative even when <see cref="Participations"/> is empty.</summary>
    [JsonPropertyName("participantCount")]
    public int ParticipantCount { get; init; }

    /// <summary>The hosting group, when there is one.</summary>
    [JsonPropertyName("group")]
    public WiseOldManGroup? Group { get; init; }

    /// <summary>The standings. Populated on the single-competition route only.</summary>
    [JsonPropertyName("participations")]
    public IReadOnlyList<WiseOldManParticipation> Participations { get; init; } = [];
}

/// <summary>One experience rate band within a skill.</summary>
public sealed record WiseOldManEfficiencyMethod
{
    /// <summary>Experience at which this rate starts applying.</summary>
    [JsonPropertyName("startExp")]
    public long StartExp { get; init; }

    /// <summary>Experience per hour.</summary>
    [JsonPropertyName("rate")]
    public double Rate { get; init; }

    /// <summary>What the rate assumes, e.g. <c>Bonus XP from Slayer</c>.</summary>
    [JsonPropertyName("description")]
    public string? Description { get; init; }
}

/// <summary>Experience one skill grants another as a by-product.</summary>
public sealed record WiseOldManEfficiencyBonus
{
    /// <summary>The skill being trained.</summary>
    [JsonPropertyName("originSkill")]
    public string OriginSkill { get; init; } = string.Empty;

    /// <summary>The skill receiving the by-product experience.</summary>
    [JsonPropertyName("bonusSkill")]
    public string BonusSkill { get; init; } = string.Empty;

    /// <summary>Experience in the origin skill at which the bonus starts.</summary>
    [JsonPropertyName("startExp")]
    public long StartExp { get; init; }

    /// <summary>Experience in the origin skill at which it stops.</summary>
    [JsonPropertyName("endExp")]
    public long EndExp { get; init; }

    /// <summary>Whether the bonus is granted at the end of the band rather than throughout.</summary>
    [JsonPropertyName("end")]
    public bool End { get; init; }

    /// <summary>Bonus experience per point of origin experience.</summary>
    [JsonPropertyName("ratio")]
    public double Ratio { get; init; }
}

/// <summary>The efficiency rates one skill is scored against.</summary>
public sealed record WiseOldManEfficiencyRate
{
    /// <summary>The skill.</summary>
    [JsonPropertyName("skill")]
    public string Skill { get; init; } = string.Empty;

    /// <summary>Rate bands, ascending by <see cref="WiseOldManEfficiencyMethod.StartExp"/>.</summary>
    [JsonPropertyName("methods")]
    public IReadOnlyList<WiseOldManEfficiencyMethod> Methods { get; init; } = [];

    /// <summary>By-product experience this skill grants others. Often empty.</summary>
    [JsonPropertyName("bonuses")]
    public IReadOnlyList<WiseOldManEfficiencyBonus> Bonuses { get; init; } = [];
}

/// <summary>The body Wise Old Man returns with a non-success status.</summary>
public sealed record WiseOldManError
{
    /// <summary>A machine-readable code, e.g. <c>PLAYER_NOT_FOUND</c>. Absent on some errors.</summary>
    [JsonPropertyName("code")]
    public string? Code { get; init; }

    /// <summary>A human-readable message.</summary>
    [JsonPropertyName("message")]
    public string? Message { get; init; }
}
