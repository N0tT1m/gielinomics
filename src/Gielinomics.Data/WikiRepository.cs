using Dapper;
using Npgsql;
using NpgsqlTypes;

namespace Gielinomics.Data;

/// <summary>A bonuses row ready to persist.</summary>
/// <param name="PageName">Wiki page.</param>
/// <param name="EquipmentSlot">Slot.</param>
/// <param name="CombatStyle">Weapon class.</param>
/// <param name="WeaponAttackSpeed">Attack speed in ticks.</param>
/// <param name="WeaponAttackRange">Attack range.</param>
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
/// <param name="MagicDamageBonus">Magic damage bonus.</param>
public readonly record struct BonusesRow(
    string PageName,
    string? EquipmentSlot,
    string? CombatStyle,
    int? WeaponAttackSpeed,
    string? WeaponAttackRange,
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

/// <summary>A drop row ready to persist.</summary>
/// <param name="ItemName">Dropped item.</param>
/// <param name="SourceName">What drops it.</param>
/// <param name="SourceVersion">Which variant of the source.</param>
/// <param name="RarityText">Rarity as written.</param>
/// <param name="Rarity">Parsed probability, or null when qualitative.</param>
/// <param name="QuantityLow">Minimum quantity.</param>
/// <param name="QuantityHigh">Maximum quantity.</param>
/// <param name="Rolls">Table rolls.</param>
/// <param name="DropType">Kind of source.</param>
/// <param name="RareDropTable">From the shared rare drop table.</param>
public readonly record struct DropRow(
    string ItemName,
    string SourceName,
    string? SourceVersion,
    string? RarityText,
    decimal? Rarity,
    int? QuantityLow,
    int? QuantityHigh,
    int? Rolls,
    string? DropType,
    bool RareDropTable);

/// <summary>A monster row ready to persist.</summary>
/// <param name="PageName">Wiki page.</param>
/// <param name="Name">Display name.</param>
/// <param name="VersionAnchor">Which variant.</param>
/// <param name="CombatLevel">Combat level.</param>
/// <param name="Hitpoints">Hitpoints.</param>
/// <param name="SlayerLevel">Slayer level required.</param>
/// <param name="SlayerExperience">Slayer experience per kill.</param>
/// <param name="Members">Members-only.</param>
public readonly record struct MonsterRow(
    string PageName,
    string? Name,
    string? VersionAnchor,
    int? CombatLevel,
    int? Hitpoints,
    int? SlayerLevel,
    double? SlayerExperience,
    bool Members);

/// <summary>Stats the gear comparison can rank on.</summary>
/// <remarks>
/// An allowlist, not a free-text column name. The stat is interpolated into the ORDER BY and
/// the projection, which a caller-supplied string would turn into an injection point that no
/// parameter binding can cover.
/// </remarks>
public static class GearStats
{
    private static readonly Dictionary<string, string> Columns = new(StringComparer.OrdinalIgnoreCase)
    {
        ["stab_attack"] = "stab_attack",
        ["slash_attack"] = "slash_attack",
        ["crush_attack"] = "crush_attack",
        ["range_attack"] = "range_attack",
        ["magic_attack"] = "magic_attack",
        ["stab_defence"] = "stab_defence",
        ["slash_defence"] = "slash_defence",
        ["crush_defence"] = "crush_defence",
        ["range_defence"] = "range_defence",
        ["magic_defence"] = "magic_defence",
        ["strength"] = "strength_bonus",
        ["ranged_strength"] = "ranged_strength_bonus",
        ["prayer"] = "prayer_bonus",
    };

    /// <summary>Every stat name the API accepts.</summary>
    public static IReadOnlyCollection<string> Names => Columns.Keys;

    /// <summary>Resolves a stat name to its column.</summary>
    /// <param name="name">The caller-supplied name.</param>
    /// <param name="column">The column, when the name is allowed.</param>
    /// <returns>True when the name is one of <see cref="Names"/>.</returns>
    public static bool TryResolve(string? name, out string column)
    {
        if (name is not null && Columns.TryGetValue(name, out var resolved))
        {
            column = resolved;
            return true;
        }

        column = string.Empty;
        return false;
    }
}

/// <summary>
/// Wiki structured data: equipment bonuses, drop tables and monsters.
/// </summary>
/// <remarks>
/// Each sync replaces a table wholesale inside a transaction. DELETE rather than TRUNCATE on
/// purpose: TRUNCATE takes an ACCESS EXCLUSIVE lock that would stall every reader for the
/// duration of the load, while a DELETE leaves MVCC to show readers the previous contents
/// until the transaction commits. The tables are small enough that the extra cost is nothing.
/// </remarks>
/// <param name="dataSource">The Postgres data source.</param>
public sealed class WikiRepository(NpgsqlDataSource dataSource)
{
    private const string InsertBonusesSql = """
        INSERT INTO item_bonuses (
            page_name, item_id, equipment_slot, combat_style, weapon_attack_speed, weapon_attack_range,
            stab_attack, slash_attack, crush_attack, range_attack, magic_attack,
            stab_defence, slash_defence, crush_defence, range_defence, magic_defence,
            strength_bonus, ranged_strength_bonus, prayer_bonus, magic_damage_bonus)
        SELECT u.page_name, i.id, u.equipment_slot, u.combat_style, u.weapon_attack_speed, u.weapon_attack_range,
               u.stab_attack, u.slash_attack, u.crush_attack, u.range_attack, u.magic_attack,
               u.stab_defence, u.slash_defence, u.crush_defence, u.range_defence, u.magic_defence,
               u.strength_bonus, u.ranged_strength_bonus, u.prayer_bonus, u.magic_damage_bonus
        FROM unnest(
            @page_name, @equipment_slot, @combat_style, @weapon_attack_speed, @weapon_attack_range,
            @stab_attack, @slash_attack, @crush_attack, @range_attack, @magic_attack,
            @stab_defence, @slash_defence, @crush_defence, @range_defence, @magic_defence,
            @strength_bonus, @ranged_strength_bonus, @prayer_bonus, @magic_damage_bonus)
        AS u(page_name, equipment_slot, combat_style, weapon_attack_speed, weapon_attack_range,
             stab_attack, slash_attack, crush_attack, range_attack, magic_attack,
             stab_defence, slash_defence, crush_defence, range_defence, magic_defence,
             strength_bonus, ranged_strength_bonus, prayer_bonus, magic_damage_bonus)
        -- Resolved against the price mapping by name, so gear joins to live prices on the same
        -- key the price API uses. is_stub rows are excluded: they have no name to match on.
        LEFT JOIN items i ON lower(i.name) = lower(u.page_name)
        """;

    private const string InsertDropsSql = """
        INSERT INTO item_drops (
            item_name, item_id, source_name, source_version, rarity_text, rarity,
            quantity_low, quantity_high, rolls, drop_type, rare_drop_table)
        SELECT u.item_name, i.id, u.source_name, u.source_version, u.rarity_text,
               -- 12 decimal places is finer than any real drop rate; the raw parse carries 28
               -- and compounds past what a decimal can represent once multiplied by a price.
               round(u.rarity, 12),
               u.quantity_low, u.quantity_high, u.rolls, u.drop_type, u.rare_drop_table
        FROM unnest(
            @item_name, @source_name, @source_version, @rarity_text, @rarity,
            @quantity_low, @quantity_high, @rolls, @drop_type, @rare_drop_table)
        AS u(item_name, source_name, source_version, rarity_text, rarity,
             quantity_low, quantity_high, rolls, drop_type, rare_drop_table)
        LEFT JOIN items i ON lower(i.name) = lower(u.item_name)
        """;

    private const string InsertMonstersSql = """
        INSERT INTO monsters (page_name, name, version_anchor, combat_level, hitpoints, slayer_level, slayer_experience, members)
        SELECT * FROM unnest(
            @page_name, @name, @version_anchor, @combat_level, @hitpoints, @slayer_level, @slayer_experience, @members)
        """;

    // Priced from the newest latest-trade row per item. Expected value is probability x mean
    // quantity x price x rolls; a null rarity (the wiki said 'Varies') propagates to a null
    // expected value rather than silently becoming zero.
    //
    // DISTINCT ON collapses the same drop appearing under several versions of one monster --
    // an abyssal demon has a Standard row and a Wilderness Slayer Cave row, and summing both
    // reports a kill as worth twice what it is. Identical item, rarity and quantity is one
    // drop from a player's point of view; a version that genuinely drops it at a different
    // rate keeps its own row.
    private const string DropsBySourceSql = """
        SELECT * FROM (
        SELECT DISTINCT ON (d.item_name, d.rarity_text, d.quantity_low, d.quantity_high, d.rolls)
               d.item_name       AS "ItemName",
               d.item_id         AS "ItemId",
               d.source_name     AS "SourceName",
               d.source_version  AS "SourceVersion",
               d.rarity_text     AS "RarityText",
               d.rarity          AS "Rarity",
               d.quantity_low    AS "QuantityLow",
               d.quantity_high   AS "QuantityHigh",
               d.rolls           AS "Rolls",
               d.drop_type       AS "DropType",
               d.rare_drop_table AS "RareDropTable",
               p.high            AS "UnitPrice",
               -- Rounded here too: gp to two decimal places is more precision than the
               -- underlying rarity justifies, and the unrounded product does not fit a decimal.
               round(d.rarity
                    * ((coalesce(d.quantity_low, 1) + coalesce(d.quantity_high, 1)) / 2.0)
                    * p.high
                    * coalesce(d.rolls, 1), 2) AS "ExpectedValue"
        FROM item_drops d
        LEFT JOIN LATERAL (
            SELECT high FROM price_latest pl
            WHERE pl.item_id = d.item_id AND pl.high IS NOT NULL
            ORDER BY pl.observed_at DESC
            LIMIT 1
        ) p ON true
        WHERE lower(d.source_name) = lower(@source)
        ORDER BY d.item_name, d.rarity_text, d.quantity_low, d.quantity_high, d.rolls, d.source_version
        ) x
        ORDER BY x."ExpectedValue" DESC NULLS LAST, x."ItemName"
        LIMIT @limit
        """;

    private const string DropSourcesForItemSql = """
        SELECT d.item_name       AS "ItemName",
               d.item_id         AS "ItemId",
               d.source_name     AS "SourceName",
               d.source_version  AS "SourceVersion",
               d.rarity_text     AS "RarityText",
               d.rarity          AS "Rarity",
               d.quantity_low    AS "QuantityLow",
               d.quantity_high   AS "QuantityHigh",
               d.rolls           AS "Rolls",
               d.drop_type       AS "DropType",
               d.rare_drop_table AS "RareDropTable",
               NULL::bigint      AS "UnitPrice",
               NULL::numeric     AS "ExpectedValue"
        FROM item_drops d
        WHERE d.item_id = @itemId
        ORDER BY d.rarity DESC NULLS LAST, d.source_name
        LIMIT @limit
        """;

    private const string BonusesForItemSql = """
        SELECT page_name             AS "PageName",
               item_id               AS "ItemId",
               equipment_slot        AS "EquipmentSlot",
               combat_style          AS "CombatStyle",
               weapon_attack_speed   AS "WeaponAttackSpeed",
               stab_attack           AS "StabAttack",
               slash_attack          AS "SlashAttack",
               crush_attack          AS "CrushAttack",
               range_attack          AS "RangeAttack",
               magic_attack          AS "MagicAttack",
               stab_defence          AS "StabDefence",
               slash_defence         AS "SlashDefence",
               crush_defence         AS "CrushDefence",
               range_defence         AS "RangeDefence",
               magic_defence         AS "MagicDefence",
               strength_bonus        AS "StrengthBonus",
               ranged_strength_bonus AS "RangedStrengthBonus",
               prayer_bonus          AS "PrayerBonus",
               magic_damage_bonus    AS "MagicDamageBonus"
        FROM item_bonuses
        WHERE item_id = @itemId
        LIMIT 1
        """;

    private readonly NpgsqlDataSource _dataSource = dataSource;

    /// <summary>
    /// Resolves item IDs for any wiki row that does not have one yet.
    /// </summary>
    /// <remarks>
    /// Runs as a pass after every load, so resolution never depends on the order the buckets
    /// were synced in or on how far the item mapping had got when a given bucket landed. It is
    /// idempotent, so a run that resolves nothing costs one statement and a run after the
    /// mapping catches up repairs everything the previous one missed.
    /// </remarks>
    /// <param name="cancellationToken">Cancels the write.</param>
    /// <returns>How many rows gained an ID.</returns>
    public async Task<int> ResolveItemIdsAsync(CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var bonuses = await connection.ExecuteAsync(new CommandDefinition(
                """
                UPDATE item_bonuses b SET item_id = i.id
                FROM items i
                WHERE b.item_id IS NULL AND i.name IS NOT NULL AND lower(i.name) = lower(b.page_name)
                """,
                cancellationToken: cancellationToken)).ConfigureAwait(false);

            var drops = await connection.ExecuteAsync(new CommandDefinition(
                """
                UPDATE item_drops d SET item_id = i.id
                FROM items i
                WHERE d.item_id IS NULL AND i.name IS NOT NULL AND lower(i.name) = lower(d.item_name)
                """,
                cancellationToken: cancellationToken)).ConfigureAwait(false);

            return bonuses + drops;
        }
    }

    /// <summary>Replaces the equipment bonuses table.</summary>
    /// <param name="rows">The full set from the wiki.</param>
    /// <param name="cancellationToken">Cancels the write.</param>
    /// <returns>Rows written.</returns>
    /// <exception cref="ArgumentNullException"><paramref name="rows"/> is null.</exception>
    public async Task<int> ReplaceBonusesAsync(IReadOnlyCollection<BonusesRow> rows, CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(rows);

        if (rows.Count == 0)
        {
            // An empty sync is a failed sync, not a wiki with no equipment in it. Refusing to
            // apply it keeps a bad upstream response from emptying the table.
            return 0;
        }

        var count = rows.Count;
        var pageNames = new string[count];
        var slots = new string?[count];
        var styles = new string?[count];
        var speeds = new int?[count];
        var ranges = new string?[count];
        var stabAttack = new int?[count];
        var slashAttack = new int?[count];
        var crushAttack = new int?[count];
        var rangeAttack = new int?[count];
        var magicAttack = new int?[count];
        var stabDefence = new int?[count];
        var slashDefence = new int?[count];
        var crushDefence = new int?[count];
        var rangeDefence = new int?[count];
        var magicDefence = new int?[count];
        var strength = new int?[count];
        var rangedStrength = new int?[count];
        var prayer = new int?[count];
        var magicDamage = new double?[count];

        var i = 0;
        foreach (var row in rows)
        {
            pageNames[i] = row.PageName;
            slots[i] = row.EquipmentSlot;
            styles[i] = row.CombatStyle;
            speeds[i] = row.WeaponAttackSpeed;
            ranges[i] = row.WeaponAttackRange;
            stabAttack[i] = row.StabAttack;
            slashAttack[i] = row.SlashAttack;
            crushAttack[i] = row.CrushAttack;
            rangeAttack[i] = row.RangeAttack;
            magicAttack[i] = row.MagicAttack;
            stabDefence[i] = row.StabDefence;
            slashDefence[i] = row.SlashDefence;
            crushDefence[i] = row.CrushDefence;
            rangeDefence[i] = row.RangeDefence;
            magicDefence[i] = row.MagicDefence;
            strength[i] = row.StrengthBonus;
            rangedStrength[i] = row.RangedStrengthBonus;
            prayer[i] = row.PrayerBonus;
            magicDamage[i] = row.MagicDamageBonus;
            i++;
        }

        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var transaction = await connection.BeginTransactionAsync(cancellationToken).ConfigureAwait(false);
            await using (transaction.ConfigureAwait(false))
            {
                await ExecuteAsync(connection, transaction, "DELETE FROM item_bonuses", cancellationToken).ConfigureAwait(false);

                var command = connection.CreateCommand();
                await using (command.ConfigureAwait(false))
                {
                    command.CommandText = InsertBonusesSql;
                    command.Transaction = (NpgsqlTransaction)transaction;
                    PriceRepository.AddArray(command, "page_name", NpgsqlDbType.Text, pageNames);
                    PriceRepository.AddArray(command, "equipment_slot", NpgsqlDbType.Text, slots);
                    PriceRepository.AddArray(command, "combat_style", NpgsqlDbType.Text, styles);
                    PriceRepository.AddArray(command, "weapon_attack_speed", NpgsqlDbType.Integer, speeds);
                    PriceRepository.AddArray(command, "weapon_attack_range", NpgsqlDbType.Text, ranges);
                    PriceRepository.AddArray(command, "stab_attack", NpgsqlDbType.Integer, stabAttack);
                    PriceRepository.AddArray(command, "slash_attack", NpgsqlDbType.Integer, slashAttack);
                    PriceRepository.AddArray(command, "crush_attack", NpgsqlDbType.Integer, crushAttack);
                    PriceRepository.AddArray(command, "range_attack", NpgsqlDbType.Integer, rangeAttack);
                    PriceRepository.AddArray(command, "magic_attack", NpgsqlDbType.Integer, magicAttack);
                    PriceRepository.AddArray(command, "stab_defence", NpgsqlDbType.Integer, stabDefence);
                    PriceRepository.AddArray(command, "slash_defence", NpgsqlDbType.Integer, slashDefence);
                    PriceRepository.AddArray(command, "crush_defence", NpgsqlDbType.Integer, crushDefence);
                    PriceRepository.AddArray(command, "range_defence", NpgsqlDbType.Integer, rangeDefence);
                    PriceRepository.AddArray(command, "magic_defence", NpgsqlDbType.Integer, magicDefence);
                    PriceRepository.AddArray(command, "strength_bonus", NpgsqlDbType.Integer, strength);
                    PriceRepository.AddArray(command, "ranged_strength_bonus", NpgsqlDbType.Integer, rangedStrength);
                    PriceRepository.AddArray(command, "prayer_bonus", NpgsqlDbType.Integer, prayer);
                    PriceRepository.AddArray(command, "magic_damage_bonus", NpgsqlDbType.Double, magicDamage);

                    var written = await command.ExecuteNonQueryAsync(cancellationToken).ConfigureAwait(false);
                    await transaction.CommitAsync(cancellationToken).ConfigureAwait(false);
                    return written;
                }
            }
        }
    }

    /// <summary>Replaces the drop table.</summary>
    /// <param name="rows">The full set from the wiki.</param>
    /// <param name="cancellationToken">Cancels the write.</param>
    /// <returns>Rows written.</returns>
    /// <exception cref="ArgumentNullException"><paramref name="rows"/> is null.</exception>
    public async Task<int> ReplaceDropsAsync(IReadOnlyCollection<DropRow> rows, CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(rows);

        if (rows.Count == 0) return 0;

        var count = rows.Count;
        var itemNames = new string[count];
        var sourceNames = new string[count];
        var sourceVersions = new string?[count];
        var rarityTexts = new string?[count];
        var rarities = new decimal?[count];
        var quantityLow = new int?[count];
        var quantityHigh = new int?[count];
        var rolls = new int?[count];
        var dropTypes = new string?[count];
        var rareTable = new bool[count];

        var i = 0;
        foreach (var row in rows)
        {
            itemNames[i] = row.ItemName;
            sourceNames[i] = row.SourceName;
            sourceVersions[i] = row.SourceVersion;
            rarityTexts[i] = row.RarityText;
            rarities[i] = row.Rarity;
            quantityLow[i] = row.QuantityLow;
            quantityHigh[i] = row.QuantityHigh;
            rolls[i] = row.Rolls;
            dropTypes[i] = row.DropType;
            rareTable[i] = row.RareDropTable;
            i++;
        }

        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var transaction = await connection.BeginTransactionAsync(cancellationToken).ConfigureAwait(false);
            await using (transaction.ConfigureAwait(false))
            {
                await ExecuteAsync(connection, transaction, "DELETE FROM item_drops", cancellationToken).ConfigureAwait(false);

                var command = connection.CreateCommand();
                await using (command.ConfigureAwait(false))
                {
                    command.CommandText = InsertDropsSql;
                    command.Transaction = (NpgsqlTransaction)transaction;
                    PriceRepository.AddArray(command, "item_name", NpgsqlDbType.Text, itemNames);
                    PriceRepository.AddArray(command, "source_name", NpgsqlDbType.Text, sourceNames);
                    PriceRepository.AddArray(command, "source_version", NpgsqlDbType.Text, sourceVersions);
                    PriceRepository.AddArray(command, "rarity_text", NpgsqlDbType.Text, rarityTexts);
                    PriceRepository.AddArray(command, "rarity", NpgsqlDbType.Numeric, rarities);
                    PriceRepository.AddArray(command, "quantity_low", NpgsqlDbType.Integer, quantityLow);
                    PriceRepository.AddArray(command, "quantity_high", NpgsqlDbType.Integer, quantityHigh);
                    PriceRepository.AddArray(command, "rolls", NpgsqlDbType.Integer, rolls);
                    PriceRepository.AddArray(command, "drop_type", NpgsqlDbType.Text, dropTypes);
                    PriceRepository.AddArray(command, "rare_drop_table", NpgsqlDbType.Boolean, rareTable);

                    var written = await command.ExecuteNonQueryAsync(cancellationToken).ConfigureAwait(false);
                    await transaction.CommitAsync(cancellationToken).ConfigureAwait(false);
                    return written;
                }
            }
        }
    }

    /// <summary>Replaces the monster table.</summary>
    /// <param name="rows">The full set from the wiki.</param>
    /// <param name="cancellationToken">Cancels the write.</param>
    /// <returns>Rows written.</returns>
    /// <exception cref="ArgumentNullException"><paramref name="rows"/> is null.</exception>
    public async Task<int> ReplaceMonstersAsync(IReadOnlyCollection<MonsterRow> rows, CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(rows);

        if (rows.Count == 0) return 0;

        var count = rows.Count;
        var pageNames = new string[count];
        var names = new string?[count];
        var versions = new string?[count];
        var combatLevels = new int?[count];
        var hitpoints = new int?[count];
        var slayerLevels = new int?[count];
        var slayerXp = new double?[count];
        var members = new bool[count];

        var i = 0;
        foreach (var row in rows)
        {
            pageNames[i] = row.PageName;
            names[i] = row.Name;
            versions[i] = row.VersionAnchor;
            combatLevels[i] = row.CombatLevel;
            hitpoints[i] = row.Hitpoints;
            slayerLevels[i] = row.SlayerLevel;
            slayerXp[i] = row.SlayerExperience;
            members[i] = row.Members;
            i++;
        }

        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var transaction = await connection.BeginTransactionAsync(cancellationToken).ConfigureAwait(false);
            await using (transaction.ConfigureAwait(false))
            {
                await ExecuteAsync(connection, transaction, "DELETE FROM monsters", cancellationToken).ConfigureAwait(false);

                var command = connection.CreateCommand();
                await using (command.ConfigureAwait(false))
                {
                    command.CommandText = InsertMonstersSql;
                    command.Transaction = (NpgsqlTransaction)transaction;
                    PriceRepository.AddArray(command, "page_name", NpgsqlDbType.Text, pageNames);
                    PriceRepository.AddArray(command, "name", NpgsqlDbType.Text, names);
                    PriceRepository.AddArray(command, "version_anchor", NpgsqlDbType.Text, versions);
                    PriceRepository.AddArray(command, "combat_level", NpgsqlDbType.Integer, combatLevels);
                    PriceRepository.AddArray(command, "hitpoints", NpgsqlDbType.Integer, hitpoints);
                    PriceRepository.AddArray(command, "slayer_level", NpgsqlDbType.Integer, slayerLevels);
                    PriceRepository.AddArray(command, "slayer_experience", NpgsqlDbType.Double, slayerXp);
                    PriceRepository.AddArray(command, "members", NpgsqlDbType.Boolean, members);

                    var written = await command.ExecuteNonQueryAsync(cancellationToken).ConfigureAwait(false);
                    await transaction.CommitAsync(cancellationToken).ConfigureAwait(false);
                    return written;
                }
            }
        }
    }

    /// <summary>A monster's drop table, priced against retained history.</summary>
    /// <param name="source">Monster name.</param>
    /// <param name="limit">Maximum rows.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The drops, most valuable per kill first.</returns>
    public async Task<IReadOnlyList<DropTableEntry>> GetDropsBySourceAsync(
        string source,
        int limit,
        CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var rows = await connection.QueryAsync<DropTableEntry>(new CommandDefinition(
                DropsBySourceSql,
                new { source, limit },
                cancellationToken: cancellationToken)).ConfigureAwait(false);

            return [.. rows];
        }
    }

    /// <summary>Everything that drops a given item.</summary>
    /// <param name="itemId">Item game ID.</param>
    /// <param name="limit">Maximum rows.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The sources, most common first.</returns>
    public async Task<IReadOnlyList<DropTableEntry>> GetDropSourcesAsync(
        int itemId,
        int limit,
        CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var rows = await connection.QueryAsync<DropTableEntry>(new CommandDefinition(
                DropSourcesForItemSql,
                new { itemId, limit },
                cancellationToken: cancellationToken)).ConfigureAwait(false);

            return [.. rows];
        }
    }

    /// <summary>Equipment bonuses for one item.</summary>
    /// <param name="itemId">Item game ID.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The bonuses, or null when the item is not equipment.</returns>
    public async Task<ItemBonuses?> GetBonusesAsync(int itemId, CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            return await connection.QuerySingleOrDefaultAsync<ItemBonuses>(new CommandDefinition(
                BonusesForItemSql,
                new { itemId },
                cancellationToken: cancellationToken)).ConfigureAwait(false);
        }
    }

    /// <summary>
    /// Ranks equipment by one stat, against its current price.
    /// </summary>
    /// <param name="stat">A name from <see cref="GearStats.Names"/>. Resolved through the allowlist.</param>
    /// <param name="slot">Equipment slot to restrict to, or null for all.</param>
    /// <param name="maxPrice">Budget ceiling, or null for no ceiling.</param>
    /// <param name="tradeableOnly">
    /// Restricts to items with a resolved game ID. On by default, because the wiki carries a
    /// cosmetic, beta or Last Man Standing variant of most notable weapons with identical
    /// bonuses and no price — four rows of "Elder maul" ahead of anything a player could buy.
    /// A comparison of price per point cannot say anything about an item that has no price.
    /// </param>
    /// <param name="cheapestFirst">
    /// Rank by gp per point rather than by raw stat. This is the question most worth asking —
    /// the biggest bonus is usually just the most expensive one — so it deserves to be an
    /// option rather than something a caller re-sorts by hand.
    /// </param>
    /// <param name="limit">Maximum rows.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The options, ordered as requested.</returns>
    /// <exception cref="ArgumentException"><paramref name="stat"/> is not on the allowlist.</exception>
    public async Task<IReadOnlyList<GearOption>> GetGearAsync(
        string stat,
        string? slot,
        long? maxPrice,
        bool cheapestFirst,
        bool tradeableOnly,
        int limit,
        CancellationToken cancellationToken = default)
    {
        if (!GearStats.TryResolve(stat, out var column))
        {
            throw new ArgumentException($"'{stat}' is not a rankable stat.", nameof(stat));
        }

        // The column comes from the allowlist above, never from the caller's string.
        //
        // DISTINCT ON collapses the variants one page carries -- a dagger's poison tiers are
        // four rows with identical bonuses, and listing them four times crowds out real
        // alternatives. A variant with genuinely different stats keeps its own row.
        var ordering = cheapestFirst
            ? """ORDER BY x."GpPerPoint" ASC NULLS LAST, x."StatValue" DESC"""
            : """ORDER BY x."StatValue" DESC, x."GpPerPoint" ASC NULLS LAST""";

        var sql = $"""
            SELECT * FROM (
            SELECT DISTINCT ON (b.page_name, b.equipment_slot, b.{column})
                   b.page_name      AS "PageName",
                   b.item_id        AS "ItemId",
                   i.name           AS "Name",
                   b.equipment_slot AS "EquipmentSlot",
                   b.{column}       AS "StatValue",
                   p.high           AS "Price",
                   CASE WHEN b.{column} > 0 AND p.high IS NOT NULL
                        THEN round(p.high::numeric / b.{column}, 2)
                   END              AS "GpPerPoint"
            FROM item_bonuses b
            LEFT JOIN items i ON i.id = b.item_id
            LEFT JOIN LATERAL (
                SELECT high FROM price_latest pl
                WHERE pl.item_id = b.item_id AND pl.high IS NOT NULL
                ORDER BY pl.observed_at DESC
                LIMIT 1
            ) p ON true
            WHERE b.{column} IS NOT NULL
              AND (NOT @tradeableOnly OR b.item_id IS NOT NULL)
              AND (@slot IS NULL OR b.equipment_slot = @slot)
              AND (@maxPrice IS NULL OR (p.high IS NOT NULL AND p.high <= @maxPrice))
            ORDER BY b.page_name, b.equipment_slot, b.{column}, b.item_id
            ) x
            {ordering}
            LIMIT @limit
            """;

        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            var rows = await connection.QueryAsync<GearOption>(new CommandDefinition(
                sql,
                new { slot, maxPrice, tradeableOnly, limit },
                cancellationToken: cancellationToken)).ConfigureAwait(false);

            return [.. rows];
        }
    }

    /// <summary>Looks up a monster by name.</summary>
    /// <param name="name">Monster name.</param>
    /// <param name="cancellationToken">Cancels the read.</param>
    /// <returns>The monster, or null when unknown.</returns>
    public async Task<Monster?> GetMonsterAsync(string name, CancellationToken cancellationToken = default)
    {
        var connection = await _dataSource.OpenConnectionAsync(cancellationToken).ConfigureAwait(false);
        await using (connection.ConfigureAwait(false))
        {
            return await connection.QuerySingleOrDefaultAsync<Monster>(new CommandDefinition(
                """
                SELECT page_name AS "PageName", name AS "Name", version_anchor AS "VersionAnchor",
                       combat_level AS "CombatLevel", hitpoints AS "Hitpoints",
                       slayer_level AS "SlayerLevel", slayer_experience AS "SlayerExperience",
                       members AS "Members"
                FROM monsters
                WHERE lower(name) = lower(@name)
                ORDER BY version_anchor NULLS FIRST
                LIMIT 1
                """,
                new { name },
                cancellationToken: cancellationToken)).ConfigureAwait(false);
        }
    }

    /// <summary>Runs a statement inside a transaction.</summary>
    /// <param name="connection">The open connection.</param>
    /// <param name="transaction">The enclosing transaction.</param>
    /// <param name="sql">The statement.</param>
    /// <param name="cancellationToken">Cancels the write.</param>
    private static async Task ExecuteAsync(
        NpgsqlConnection connection,
        System.Data.Common.DbTransaction transaction,
        string sql,
        CancellationToken cancellationToken)
    {
        var command = connection.CreateCommand();
        await using (command.ConfigureAwait(false))
        {
            command.CommandText = sql;
            command.Transaction = (NpgsqlTransaction)transaction;
            await command.ExecuteNonQueryAsync(cancellationToken).ConfigureAwait(false);
        }
    }
}
