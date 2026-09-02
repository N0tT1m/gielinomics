namespace Gielinomics.Client.Hiscores;

/// <summary>
/// The index-to-name mapping for the positional CSV endpoint, and the known-name set the
/// schema drift alarm compares against.
/// </summary>
/// <remarks>
/// <para>
/// Versioned rather than hardcoded-and-forgotten. Jagex appends new skills and bosses on game
/// updates without notice — <c>Sailing</c> is the most recent — and with the CSV endpoint the
/// ordering is the entire contract: one insertion misattributes every field after it.
/// </para>
/// <para>
/// <see cref="Version"/> is stored on every snapshot so a row decoded under an old mapping
/// stays interpretable after the mapping moves.
/// </para>
/// </remarks>
public sealed record HiscoreMapping
{
    /// <summary>Identifies which mapping decoded a snapshot. Increment on any change below.</summary>
    public required int Version { get; init; }

    /// <summary>Skill names, in CSV line order.</summary>
    public required IReadOnlyList<string> SkillNames { get; init; }

    /// <summary>Activity names, in CSV line order after the skills.</summary>
    public required IReadOnlyList<string> ActivityNames { get; init; }

    /// <summary>Total lines a complete CSV response should carry.</summary>
    public int ExpectedLineCount => SkillNames.Count + ActivityNames.Count;

    /// <summary>
    /// The mapping in force, verified live against <c>index_lite.json</c> on 2 September 2026.
    /// </summary>
    public static HiscoreMapping Current { get; } = new()
    {
        Version = 1,
        SkillNames =
        [
            "Overall", "Attack", "Defence", "Strength", "Hitpoints", "Ranged", "Prayer", "Magic",
            "Cooking", "Woodcutting", "Fletching", "Fishing", "Firemaking", "Crafting", "Smithing",
            "Mining", "Herblore", "Agility", "Thieving", "Slayer", "Farming", "Runecraft", "Hunter",
            "Construction", "Sailing",
        ],
        ActivityNames =
        [
            "Grid Points", "League Points", "Deadman Points", "Bounty Hunter - Hunter",
            "Bounty Hunter - Rogue", "Bounty Hunter (Legacy) - Hunter", "Bounty Hunter (Legacy) - Rogue",
            "Clue Scrolls (all)", "Clue Scrolls (beginner)", "Clue Scrolls (easy)",
            "Clue Scrolls (medium)", "Clue Scrolls (hard)", "Clue Scrolls (elite)",
            "Clue Scrolls (master)", "LMS - Rank", "PvP Arena - Rank", "Soul Wars Zeal", "Rifts closed",
            "Colosseum Glory", "Collections Logged", "Abyssal Sire", "Alchemical Hydra", "Amoxliatl",
            "Araxxor", "Artio", "Barrows Chests", "Brutus", "Bryophyta", "Callisto", "Calvar'ion",
            "Cerberus", "Chambers of Xeric", "Chambers of Xeric: Challenge Mode", "Chaos Elemental",
            "Chaos Fanatic", "Commander Zilyana", "Corporeal Beast", "Crazy Archaeologist",
            "Dagannoth Prime", "Dagannoth Rex", "Dagannoth Supreme", "Deranged Archaeologist",
            "Doom of Mokhaiotl", "Duke Sucellus", "General Graardor", "Giant Mole", "Grotesque Guardians",
            "Hespori", "Kalphite Queen", "King Black Dragon", "Kraken", "Kree'Arra", "K'ril Tsutsaroth",
            "Lunar Chests", "Mad Angel", "Maggot King", "Mimic", "Nex", "Nightmare", "Phosani's Nightmare",
            "Obor", "Phantom Muspah", "Sarachnis", "Scorpia", "Scurrius", "Shellbane Gryphon", "Skotizo",
            "Sol Heredit", "Spindel", "Tempoross", "The Gauntlet", "The Corrupted Gauntlet",
            "The Hueycoatl", "The Leviathan", "The Royal Titans", "The Whisperer", "Theatre of Blood",
            "Theatre of Blood: Hard Mode", "Thermonuclear Smoke Devil", "Tombs of Amascut",
            "Tombs of Amascut: Expert Mode", "TzKal-Zuk", "TzTok-Jad", "Vardorvis", "Venenatis", "Vet'ion",
            "Vorkath", "Wintertodt", "Yama", "Zalcano", "Zulrah"
        ],
    };

    /// <summary>Whether a name is one this mapping knows about.</summary>
    /// <param name="name">A skill or activity name from a response.</param>
    /// <returns>True when the mapping already accounts for it.</returns>
    public bool Knows(string name)
        => SkillNames.Contains(name, StringComparer.OrdinalIgnoreCase)
        || ActivityNames.Contains(name, StringComparer.OrdinalIgnoreCase);
}
