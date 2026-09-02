using System.Text.Json.Serialization;

namespace Gielinomics.Client.Wiki;

/// <summary>The envelope every Bucket response comes in.</summary>
/// <typeparam name="T">The row type.</typeparam>
public sealed record BucketEnvelope<T>
{
    /// <summary>The query the server echoed back.</summary>
    [JsonPropertyName("bucketQuery")]
    public string? BucketQuery { get; init; }

    /// <summary>The rows, or null when the query failed.</summary>
    [JsonPropertyName("bucket")]
    public IReadOnlyList<T>? Bucket { get; init; }

    /// <summary>
    /// The failure message.
    /// </summary>
    /// <remarks>
    /// Bucket reports query errors with HTTP 200 and this field set, so a status check alone
    /// would read a Lua syntax error as a successful empty sync.
    /// </remarks>
    [JsonPropertyName("error")]
    public string? Error { get; init; }
}

/// <summary>
/// A row from <c>infobox_item</c>.
/// </summary>
/// <remarks>
/// One wiki page can carry several rows — an item with variants, or a quest-specific version —
/// distinguished by <see cref="VersionAnchor"/>. <see cref="PageName"/> is Bucket's implicit
/// per-page key and the join back to everything else.
/// </remarks>
public sealed record BucketItem
{
    /// <summary>The wiki page this row belongs to.</summary>
    [JsonPropertyName("page_name")]
    public string PageName { get; init; } = string.Empty;

    /// <summary>Display name.</summary>
    [JsonPropertyName("item_name")]
    public string? ItemName { get; init; }

    /// <summary>
    /// Game IDs for this row, as strings.
    /// </summary>
    /// <remarks>
    /// Repeated and stringly-typed on the wire even when there is exactly one. A stacked or
    /// charged item legitimately maps to several IDs.
    /// </remarks>
    [JsonPropertyName("item_id")]
    public IReadOnlyList<string>? ItemId { get; init; }

    /// <summary>Which variant of the page this row describes.</summary>
    [JsonPropertyName("version_anchor")]
    public string? VersionAnchor { get; init; }

    /// <summary>High alchemy value.</summary>
    [JsonPropertyName("high_alchemy_value")]
    public long? HighAlchemyValue { get; init; }

    /// <summary>Grand Exchange buy limit.</summary>
    [JsonPropertyName("buy_limit")]
    public int? BuyLimit { get; init; }

    /// <summary>Members flag. See <see cref="BucketFlags"/> for why this is a string.</summary>
    [JsonPropertyName("is_members_only")]
    public string? IsMembersOnly { get; init; }

    /// <summary>Tradeable flag.</summary>
    [JsonPropertyName("tradeable")]
    public string? Tradeable { get; init; }

    /// <summary>Whether this is the page's primary variant.</summary>
    [JsonPropertyName("default_version")]
    public string? DefaultVersion { get; init; }
}

/// <summary>Equipment bonuses from <c>infobox_bonuses</c> — the gear comparison surface.</summary>
public sealed record BucketBonuses
{
    /// <summary>The wiki page this row belongs to.</summary>
    [JsonPropertyName("page_name")]
    public string PageName { get; init; } = string.Empty;

    /// <summary>Which slot the item occupies.</summary>
    [JsonPropertyName("equipment_slot")]
    public string? EquipmentSlot { get; init; }

    /// <summary>Weapon class, for weapons.</summary>
    [JsonPropertyName("combat_style")]
    public string? CombatStyle { get; init; }

    /// <summary>Attack speed in ticks, for weapons.</summary>
    [JsonPropertyName("weapon_attack_speed")]
    public int? WeaponAttackSpeed { get; init; }

    /// <summary>Attack range, for weapons.</summary>
    [JsonPropertyName("weapon_attack_range")]
    public string? WeaponAttackRange { get; init; }

    /// <summary>Stab attack bonus.</summary>
    [JsonPropertyName("stab_attack_bonus")]
    public int? StabAttack { get; init; }

    /// <summary>Slash attack bonus.</summary>
    [JsonPropertyName("slash_attack_bonus")]
    public int? SlashAttack { get; init; }

    /// <summary>Crush attack bonus.</summary>
    [JsonPropertyName("crush_attack_bonus")]
    public int? CrushAttack { get; init; }

    /// <summary>Ranged attack bonus.</summary>
    [JsonPropertyName("range_attack_bonus")]
    public int? RangeAttack { get; init; }

    /// <summary>Magic attack bonus.</summary>
    [JsonPropertyName("magic_attack_bonus")]
    public int? MagicAttack { get; init; }

    /// <summary>Stab defence bonus.</summary>
    [JsonPropertyName("stab_defence_bonus")]
    public int? StabDefence { get; init; }

    /// <summary>Slash defence bonus.</summary>
    [JsonPropertyName("slash_defence_bonus")]
    public int? SlashDefence { get; init; }

    /// <summary>Crush defence bonus.</summary>
    [JsonPropertyName("crush_defence_bonus")]
    public int? CrushDefence { get; init; }

    /// <summary>Ranged defence bonus.</summary>
    [JsonPropertyName("range_defence_bonus")]
    public int? RangeDefence { get; init; }

    /// <summary>Magic defence bonus.</summary>
    [JsonPropertyName("magic_defence_bonus")]
    public int? MagicDefence { get; init; }

    /// <summary>Melee strength bonus.</summary>
    [JsonPropertyName("strength_bonus")]
    public int? StrengthBonus { get; init; }

    /// <summary>Ranged strength bonus.</summary>
    [JsonPropertyName("ranged_strength_bonus")]
    public int? RangedStrengthBonus { get; init; }

    /// <summary>Prayer bonus.</summary>
    [JsonPropertyName("prayer_bonus")]
    public int? PrayerBonus { get; init; }

    /// <summary>Magic damage bonus, as a percentage.</summary>
    [JsonPropertyName("magic_damage_bonus")]
    public double? MagicDamageBonus { get; init; }
}

/// <summary>A row from <c>dropsline</c>. The detail lives in <see cref="DropJson"/>.</summary>
public sealed record BucketDrop
{
    /// <summary>The dropped item's name.</summary>
    [JsonPropertyName("item_name")]
    public string ItemName { get; init; } = string.Empty;

    /// <summary>The drop's details, as an embedded JSON document.</summary>
    [JsonPropertyName("drop_json")]
    public string? DropJson { get; init; }

    /// <summary>Whether this drop comes from the shared rare drop table.</summary>
    [JsonPropertyName("rare_drop_table")]
    public string? RareDropTable { get; init; }
}

/// <summary>The decoded contents of <see cref="BucketDrop.DropJson"/>.</summary>
public sealed record DropDetail
{
    /// <summary>The item dropped.</summary>
    [JsonPropertyName("Dropped item")]
    public string? DroppedItem { get; init; }

    /// <summary>The source, as <c>Monster#Version</c>.</summary>
    [JsonPropertyName("Dropped from")]
    public string? DroppedFrom { get; init; }

    /// <summary>Rarity as the wiki writes it: <c>1/512</c>, <c>Always</c>, <c>Varies</c>.</summary>
    [JsonPropertyName("Rarity")]
    public string? Rarity { get; init; }

    /// <summary>Minimum quantity per drop.</summary>
    [JsonPropertyName("Quantity Low")]
    public int? QuantityLow { get; init; }

    /// <summary>Maximum quantity per drop.</summary>
    [JsonPropertyName("Quantity High")]
    public int? QuantityHigh { get; init; }

    /// <summary>How many times the table is rolled.</summary>
    [JsonPropertyName("Rolls")]
    public int? Rolls { get; init; }

    /// <summary>What kind of source this is: combat, thieving, reward, and so on.</summary>
    [JsonPropertyName("Drop type")]
    public string? DropType { get; init; }

    /// <summary>Combat level of the source, where it has one.</summary>
    [JsonPropertyName("Drop level")]
    public string? DropLevel { get; init; }
}

/// <summary>A row from <c>infobox_monster</c>.</summary>
public sealed record BucketMonster
{
    /// <summary>The wiki page this row belongs to.</summary>
    [JsonPropertyName("page_name")]
    public string PageName { get; init; } = string.Empty;

    /// <summary>Display name.</summary>
    [JsonPropertyName("name")]
    public string? Name { get; init; }

    /// <summary>Which variant of the page this row describes.</summary>
    [JsonPropertyName("version_anchor")]
    public string? VersionAnchor { get; init; }

    /// <summary>Combat level.</summary>
    [JsonPropertyName("combat_level")]
    public int? CombatLevel { get; init; }

    /// <summary>Hitpoints.</summary>
    [JsonPropertyName("hitpoints")]
    public int? Hitpoints { get; init; }

    /// <summary>Slayer level required to be assigned this monster.</summary>
    [JsonPropertyName("slayer_level")]
    public int? SlayerLevel { get; init; }

    /// <summary>Slayer experience per kill.</summary>
    [JsonPropertyName("slayer_experience")]
    public double? SlayerExperience { get; init; }

    /// <summary>Members flag.</summary>
    [JsonPropertyName("is_members_only")]
    public string? IsMembersOnly { get; init; }

    /// <summary>Whether this is the page's primary variant.</summary>
    [JsonPropertyName("default_version")]
    public string? DefaultVersion { get; init; }
}

/// <summary>
/// Reads Bucket's boolean encoding.
/// </summary>
/// <remarks>
/// Bucket does not send <c>true</c> and <c>false</c>. A set flag arrives as an <b>empty
/// string</b> and an unset one is <b>omitted from the row entirely</b>. Modelling these as
/// <c>bool?</c> looks obvious and fails on every single row, because <c>""</c> is not a
/// boolean — so the flag is carried as a string and interpreted here.
/// </remarks>
public static class BucketFlags
{
    /// <summary>Whether a Bucket boolean field is set.</summary>
    /// <param name="value">The raw field value, or null when the field was absent.</param>
    /// <returns>True when the flag is set.</returns>
    public static bool IsSet(string? value)
        => value is not null && !string.Equals(value, "false", StringComparison.OrdinalIgnoreCase) && value != "0";
}
