namespace Gielinomics.Client.Hiscores;

/// <summary>
/// Typed access to the official Old School RuneScape hiscores.
/// </summary>
/// <remarks>
/// <para>
/// Jagex publishes no rate limit and is considerably less forgiving than the wiki. Cap at
/// roughly one request per second and cache aggressively.
/// </para>
/// <para>
/// The endpoint sends no CORS headers, so a browser cannot call it directly. That is the
/// concrete reason the query API exists: the frontend proxies through it.
/// </para>
/// </remarks>
public interface IHiscoresClient
{
    /// <summary>
    /// Fetches a player's standing on one table.
    /// </summary>
    /// <remarks>
    /// Returns null for a 404, which the API uses for two different things: the player does
    /// not exist, or the player exists but is not on <b>this</b> table. Account type detection
    /// depends on that second meaning, so a 404 is a result rather than an error.
    /// </remarks>
    /// <param name="player">Display name. Case-insensitive; spaces are encoded for you.</param>
    /// <param name="table">Which table to query.</param>
    /// <param name="cancellationToken">Cancels the request.</param>
    /// <returns>The profile, or null when the player is not on that table.</returns>
    Task<HiscoreProfile?> GetAsync(string player, HiscoreTable table = HiscoreTable.Main, CancellationToken cancellationToken = default);

    /// <summary>
    /// Infers an account's type by probing tables.
    /// </summary>
    /// <remarks>
    /// The API does not report account type. Probing in
    /// <see cref="HiscoreTables.DetectionOrder"/> — most specific first — is what
    /// distinguishes an ultimate ironman from the plain ironman table it also appears on.
    /// Costs up to one request per table, so do this once per account, not once per poll.
    /// </remarks>
    /// <param name="player">Display name.</param>
    /// <param name="cancellationToken">Cancels the requests.</param>
    /// <returns>The most specific table the player appears on, or null if they appear on none.</returns>
    Task<HiscoreTable?> DetectAccountTypeAsync(string player, CancellationToken cancellationToken = default);
}
