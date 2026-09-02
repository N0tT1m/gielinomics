namespace Gielinomics.Client.Hiscores;

/// <summary>
/// The hiscore table an account appears on.
/// </summary>
/// <remarks>
/// Account type is not returned by the API. It is inferred from which tables answer with a
/// 200 — a main queried against the ironman table gets a 404 — which is what
/// <see cref="IHiscoresClient.DetectAccountTypeAsync"/> does.
/// </remarks>
public enum HiscoreTable
{
    /// <summary>Every account appears here, whatever its type.</summary>
    Main,

    /// <summary>Standard ironman.</summary>
    Ironman,

    /// <summary>Hardcore ironman. Disappears from this table on death.</summary>
    HardcoreIronman,

    /// <summary>Ultimate ironman.</summary>
    UltimateIronman,

    /// <summary>Deadman mode.</summary>
    Deadman,

    /// <summary>Seasonal and Leagues.</summary>
    Seasonal,

    /// <summary>Tournament worlds.</summary>
    Tournament,

    /// <summary>Skiller — combat level 3.</summary>
    Skiller,

    /// <summary>Skiller with one defence level.</summary>
    SkillerDefence,

    /// <summary>Fresh Start Worlds.</summary>
    FreshStart,
}

/// <summary>Wire names for <see cref="HiscoreTable"/>.</summary>
public static class HiscoreTables
{
    /// <summary>
    /// The order account type detection probes tables in.
    /// </summary>
    /// <remarks>
    /// Most specific first. An ultimate ironman appears on the ironman table too, so probing
    /// in the other order would classify every ultimate as a plain ironman.
    /// </remarks>
    public static IReadOnlyList<HiscoreTable> DetectionOrder { get; } =
    [
        HiscoreTable.HardcoreIronman,
        HiscoreTable.UltimateIronman,
        HiscoreTable.Ironman,
        HiscoreTable.Skiller,
        HiscoreTable.SkillerDefence,
        HiscoreTable.FreshStart,
        HiscoreTable.Main,
    ];

    /// <summary>Maps a table to its <c>m=</c> path segment.</summary>
    /// <param name="table">The table.</param>
    /// <returns>The wire value.</returns>
    /// <exception cref="ArgumentOutOfRangeException">The table is not one of the known values.</exception>
    public static string ToWireValue(this HiscoreTable table) => table switch
    {
        HiscoreTable.Main => "hiscore_oldschool",
        HiscoreTable.Ironman => "hiscore_oldschool_ironman",
        HiscoreTable.HardcoreIronman => "hiscore_oldschool_hardcore_ironman",
        HiscoreTable.UltimateIronman => "hiscore_oldschool_ultimate",
        HiscoreTable.Deadman => "hiscore_oldschool_deadman",
        HiscoreTable.Seasonal => "hiscore_oldschool_seasonal",
        HiscoreTable.Tournament => "hiscore_oldschool_tournament",
        HiscoreTable.Skiller => "hiscore_oldschool_skiller",
        HiscoreTable.SkillerDefence => "hiscore_oldschool_skiller_defence",
        HiscoreTable.FreshStart => "hiscore_oldschool_fresh_start",
        _ => throw new ArgumentOutOfRangeException(nameof(table), table, "Unknown hiscore table."),
    };
}
