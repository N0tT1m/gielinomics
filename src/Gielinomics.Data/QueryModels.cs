namespace Gielinomics.Data;

/// <summary>An item as it appears in a search result.</summary>
/// <param name="Id">Item game ID.</param>
/// <param name="Name">Display name, null while the row is still a stub.</param>
/// <param name="Members">Members-only flag.</param>
/// <param name="BuyLimit">Buy limit per 4 hours.</param>
/// <param name="HighAlch">High alchemy value.</param>
/// <param name="Icon">Wiki icon filename.</param>
/// <param name="IsStub">True when the ID was seen in a price feed before <c>/mapping</c> knew about it.</param>
public sealed record ItemSummary(
    int Id,
    string? Name,
    bool? Members,
    int? BuyLimit,
    long? HighAlch,
    string? Icon,
    bool IsStub);

/// <summary>Full reference metadata for one item.</summary>
/// <param name="Id">Item game ID.</param>
/// <param name="Name">Display name.</param>
/// <param name="Examine">Examine text.</param>
/// <param name="Members">Members-only flag.</param>
/// <param name="BuyLimit">Buy limit per 4 hours.</param>
/// <param name="Value">Base store value.</param>
/// <param name="LowAlch">Low alchemy value.</param>
/// <param name="HighAlch">High alchemy value.</param>
/// <param name="Icon">Wiki icon filename.</param>
/// <param name="IsStub">Whether the mapping sync has caught up with this ID yet.</param>
/// <param name="FirstSeen">When this platform first observed the ID.</param>
/// <param name="LastSeen">When this platform last observed the ID.</param>
public sealed record ItemDetail(
    int Id,
    string? Name,
    string? Examine,
    bool? Members,
    int? BuyLimit,
    long? Value,
    long? LowAlch,
    long? HighAlch,
    string? Icon,
    bool IsStub,
    DateTimeOffset FirstSeen,
    DateTimeOffset LastSeen);

/// <summary>One retained price bar.</summary>
/// <param name="BucketTs">Window start.</param>
/// <param name="AvgHigh">Volume-weighted average instant-buy, null when nothing traded.</param>
/// <param name="AvgLow">Volume-weighted average instant-sell, null when nothing traded.</param>
/// <param name="HighVolume">Instant-buy volume.</param>
/// <param name="LowVolume">Instant-sell volume.</param>
public sealed record PricePoint(
    DateTimeOffset BucketTs,
    decimal? AvgHigh,
    decimal? AvgLow,
    long HighVolume,
    long LowVolume);

/// <summary>
/// Derived statistics over an item's retained history.
/// </summary>
/// <remarks>
/// These are the questions the upstream API cannot answer, because it does not keep the
/// history they are computed from. That is the whole product.
/// </remarks>
/// <param name="ItemId">Item game ID.</param>
/// <param name="StepSeconds">Granularity the statistics were computed at.</param>
/// <param name="From">Start of the analysed window.</param>
/// <param name="Samples">Bars with a recorded instant-buy price.</param>
/// <param name="MeanHigh">Mean instant-buy price.</param>
/// <param name="StdDevHigh">Sample standard deviation of the instant-buy price.</param>
/// <param name="Volatility">Coefficient of variation — standard deviation over the mean, so items of different price scales compare.</param>
/// <param name="MeanSpread">Mean of (high - low) / high across bars where both sides traded.</param>
/// <param name="HighVolume">Total instant-buy volume.</param>
/// <param name="LowVolume">Total instant-sell volume.</param>
public sealed record ItemStats(
    int ItemId,
    int StepSeconds,
    DateTimeOffset From,
    long Samples,
    decimal? MeanHigh,
    decimal? StdDevHigh,
    decimal? Volatility,
    decimal? MeanSpread,
    long HighVolume,
    long LowVolume);

/// <summary>An item ranked by price movement over a window.</summary>
/// <param name="ItemId">Item game ID.</param>
/// <param name="Name">Display name.</param>
/// <param name="StartPrice">First recorded instant-buy in the window.</param>
/// <param name="EndPrice">Last recorded instant-buy in the window.</param>
/// <param name="ChangePercent">Percentage change between them.</param>
/// <param name="Volume">Total traded volume over the window.</param>
public sealed record MarketMover(
    int ItemId,
    string? Name,
    decimal StartPrice,
    decimal EndPrice,
    decimal ChangePercent,
    long Volume);

/// <summary>
/// A raw buy/sell spread, before tax.
/// </summary>
/// <remarks>
/// Deliberately pre-tax. The tax rules live in <c>Gielinomics.Alerts</c>, which references
/// this project rather than the other way round, so the API applies them on the way out.
/// </remarks>
/// <param name="ItemId">Item game ID.</param>
/// <param name="Name">Display name.</param>
/// <param name="High">Latest instant-buy price — what you would pay.</param>
/// <param name="Low">Latest instant-sell price — what you would receive.</param>
/// <param name="BuyLimit">Buy limit per 4 hours, which caps how much of the margin is reachable.</param>
/// <param name="HighVolume">Recent instant-buy volume.</param>
/// <param name="LowVolume">Recent instant-sell volume.</param>
/// <param name="ObservedAt">When the latest prices were seen.</param>
public sealed record SpreadCandidate(
    int ItemId,
    string? Name,
    long High,
    long Low,
    int? BuyLimit,
    long HighVolume,
    long LowVolume,
    DateTimeOffset ObservedAt);

/// <summary>Health of one ingest feed.</summary>
/// <param name="Source">Feed name.</param>
/// <param name="LastSuccessAt">When it last completed successfully.</param>
/// <param name="SinceLastSuccess">Age of that success, measured by the database's clock.</param>
/// <param name="RunsLastDay">Attempts in the last 24 hours.</param>
/// <param name="FailuresLastDay">Attempts in the last 24 hours that did not succeed.</param>
public sealed record FeedStatus(
    string Source,
    DateTimeOffset? LastSuccessAt,
    TimeSpan? SinceLastSuccess,
    long RunsLastDay,
    long FailuresLastDay);

/// <summary>
/// How much of a window this platform actually holds.
/// </summary>
/// <remarks>
/// The number that makes the dataset trustworthy. "We have a year of history" means nothing
/// without the fraction of expected windows that are genuinely present.
/// </remarks>
/// <param name="StepSeconds">Granularity inspected.</param>
/// <param name="From">Start of the inspected window.</param>
/// <param name="To">End of the inspected window.</param>
/// <param name="ExpectedWindows">Windows that should exist over that span.</param>
/// <param name="PresentWindows">Windows that do exist.</param>
/// <param name="Coverage">Present over expected, clamped to 1.</param>
public sealed record CoverageReport(
    int StepSeconds,
    DateTimeOffset From,
    DateTimeOffset To,
    long ExpectedWindows,
    long PresentWindows,
    double Coverage);

/// <summary>An authenticated caller.</summary>
/// <param name="Id">Row ID.</param>
/// <param name="Label">Human-readable label for the token.</param>
public sealed record ApiUser(long Id, string Label);

/// <summary>A configured alert rule.</summary>
/// <param name="Id">Row ID.</param>
/// <param name="OwnerId">Owning <see cref="ApiUser"/>.</param>
/// <param name="Kind">Rule kind.</param>
/// <param name="Config">Rule configuration, as raw JSON.</param>
/// <param name="WebhookUrl">Validated Discord webhook target.</param>
/// <param name="Enabled">Whether the rule is evaluated.</param>
/// <param name="LastFired">When it last dispatched.</param>
/// <param name="CreatedAt">When it was created.</param>
public sealed record AlertRule(
    long Id,
    long OwnerId,
    string Kind,
    string Config,
    string WebhookUrl,
    bool Enabled,
    DateTimeOffset? LastFired,
    DateTimeOffset CreatedAt);

/// <summary>An item ID paired with its display name.</summary>
/// <param name="Id">Item game ID.</param>
/// <param name="Name">Display name, null while the row is still a stub.</param>
public sealed record ItemName(int Id, string? Name);

/// <summary>A tracked account.</summary>
/// <param name="Id">Stable internal ID. Names change; this does not.</param>
/// <param name="DisplayName">Current display name.</param>
/// <param name="NormalisedName">Lowercased, whitespace-normalised name used for lookup.</param>
/// <param name="AccountType">Inferred hiscore table.</param>
/// <param name="Tracked">Whether the poller includes this account.</param>
/// <param name="AddedAt">When tracking began.</param>
public sealed record Player(
    long Id,
    string DisplayName,
    string NormalisedName,
    string AccountType,
    bool Tracked,
    DateTimeOffset AddedAt);

/// <summary>One skill's standing at one capture.</summary>
/// <param name="CapturedAt">When the standing was first observed.</param>
/// <param name="Skill">Positional skill index.</param>
/// <param name="Rank">Rank, or null when unranked.</param>
/// <param name="Level">Level, or null when unranked.</param>
/// <param name="Xp">Experience, or null when unranked.</param>
public sealed record SkillSample(
    DateTimeOffset CapturedAt,
    short Skill,
    int? Rank,
    short? Level,
    long? Xp);

/// <summary>Movement in one skill over a window.</summary>
/// <param name="Skill">Positional skill index.</param>
/// <param name="Name">Skill name, decoded from the snapshot's mapping version.</param>
/// <param name="StartXp">Experience at the start of the window.</param>
/// <param name="EndXp">Experience at the end.</param>
/// <param name="GainedXp">The difference.</param>
/// <param name="StartLevel">Level at the start.</param>
/// <param name="EndLevel">Level at the end.</param>
/// <param name="GainedLevels">Levels gained over the window.</param>
public sealed record SkillGain(
    short Skill,
    string? Name,
    long? StartXp,
    long? EndXp,
    long GainedXp,
    short? StartLevel,
    short? EndLevel,
    int GainedLevels);

/// <summary>A previous or current name for a tracked account.</summary>
/// <param name="Name">The display name as seen.</param>
/// <param name="SeenFrom">First observation.</param>
/// <param name="SeenTo">Last observation, or null while current.</param>
public sealed record PlayerName(string Name, DateTimeOffset SeenFrom, DateTimeOffset? SeenTo);

/// <summary>Equipment bonuses for one item, as the wiki records them.</summary>
/// <param name="PageName">Wiki page the stats came from.</param>
/// <param name="ItemId">Resolved game ID, or null when the item is untradeable or unmapped.</param>
/// <param name="EquipmentSlot">Slot the item occupies.</param>
/// <param name="CombatStyle">Weapon class, for weapons.</param>
/// <param name="WeaponAttackSpeed">Attack speed in game ticks, for weapons.</param>
/// <param name="StabAttack">Stab attack bonus.</param>
/// <param name="SlashAttack">Slash attack bonus.</param>
/// <param name="CrushAttack">Crush attack bonus.</param>
/// <param name="RangeAttack">Ranged attack bonus.</param>
/// <param name="MagicAttack">Magic attack bonus.</param>
/// <param name="StabDefence">Stab defence bonus.</param>
/// <param name="SlashDefence">Slash defence bonus.</param>
/// <param name="CrushDefence">Crush defence bonus.</param>
/// <param name="RangeDefence">Ranged defence bonus.</param>
/// <param name="MagicDefence">Magic defence bonus.</param>
/// <param name="StrengthBonus">Melee strength bonus.</param>
/// <param name="RangedStrengthBonus">Ranged strength bonus.</param>
/// <param name="PrayerBonus">Prayer bonus.</param>
/// <param name="MagicDamageBonus">Magic damage bonus, as a percentage.</param>
public sealed record ItemBonuses(
    string PageName,
    int? ItemId,
    string? EquipmentSlot,
    string? CombatStyle,
    int? WeaponAttackSpeed,
    int? StabAttack,
    int? SlashAttack,
    int? CrushAttack,
    int? RangeAttack,
    int? MagicAttack,
    int? StabDefence,
    int? SlashDefence,
    int? CrushDefence,
    int? RangeDefence,
    int? MagicDefence,
    int? StrengthBonus,
    int? RangedStrengthBonus,
    int? PrayerBonus,
    double? MagicDamageBonus);

/// <summary>
/// One line of a drop table, priced against retained market data.
/// </summary>
/// <param name="ItemName">The dropped item.</param>
/// <param name="ItemId">Resolved game ID, or null when the drop is untradeable.</param>
/// <param name="SourceName">What drops it.</param>
/// <param name="SourceVersion">Which variant of the source, where the wiki distinguishes them.</param>
/// <param name="RarityText">Rarity exactly as the wiki writes it.</param>
/// <param name="Rarity">
/// Parsed probability, or null when the wiki's text is qualitative — 'Varies', 'Unknown',
/// 'Rare'. Null propagates into <paramref name="ExpectedValue"/> rather than being treated as
/// zero, because an invented probability turns into a gp figure somebody would act on.
/// </param>
/// <param name="QuantityLow">Minimum quantity per drop.</param>
/// <param name="QuantityHigh">Maximum quantity per drop.</param>
/// <param name="Rolls">How many times the table is rolled.</param>
/// <param name="DropType">Kind of source: combat, thieving, reward, and so on.</param>
/// <param name="RareDropTable">Whether the drop comes from the shared rare drop table.</param>
/// <param name="UnitPrice">Latest instant-buy price, from retained history.</param>
/// <param name="ExpectedValue">Probability times mean quantity times price times rolls.</param>
public sealed record DropTableEntry(
    string ItemName,
    int? ItemId,
    string SourceName,
    string? SourceVersion,
    string? RarityText,
    decimal? Rarity,
    int? QuantityLow,
    int? QuantityHigh,
    int? Rolls,
    string? DropType,
    bool RareDropTable,
    long? UnitPrice,
    decimal? ExpectedValue);

/// <summary>A monster's reference data.</summary>
/// <param name="PageName">Wiki page.</param>
/// <param name="Name">Display name.</param>
/// <param name="VersionAnchor">Which variant of the page.</param>
/// <param name="CombatLevel">Combat level.</param>
/// <param name="Hitpoints">Hitpoints.</param>
/// <param name="SlayerLevel">Slayer level required.</param>
/// <param name="SlayerExperience">Slayer experience per kill.</param>
/// <param name="Members">Members-only.</param>
public sealed record Monster(
    string PageName,
    string? Name,
    string? VersionAnchor,
    int? CombatLevel,
    int? Hitpoints,
    int? SlayerLevel,
    double? SlayerExperience,
    bool Members);

/// <summary>An item ranked by an equipment stat against its current price.</summary>
/// <param name="PageName">Wiki page.</param>
/// <param name="ItemId">Game ID.</param>
/// <param name="Name">Item name from the price mapping.</param>
/// <param name="EquipmentSlot">Slot.</param>
/// <param name="StatValue">The stat being ranked on.</param>
/// <param name="Price">Latest instant-buy price.</param>
/// <param name="GpPerPoint">
/// Price divided by the stat. Null when the stat is zero or negative, or the price is unknown —
/// dividing by a zero bonus produces an infinity that would sort to the top.
/// </param>
public sealed record GearOption(
    string PageName,
    int? ItemId,
    string? Name,
    string? EquipmentSlot,
    int StatValue,
    long? Price,
    decimal? GpPerPoint);
