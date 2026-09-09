namespace Gielinomics.Client.WiseOldMan;

/// <summary>
/// Typed access to the <a href="https://wiseoldman.net">Wise Old Man</a> v2 API.
/// </summary>
/// <remarks>
/// <para>
/// <b>Read-only.</b> Wise Old Man exposes write routes — tracking a player forces a hiscore
/// fetch on their infrastructure, and group and competition routes mutate other people's data.
/// None of them are here. A client that can only read cannot spend somebody else's budget by
/// accident, and this package's consumers want the history, not the ability to edit it.
/// </para>
/// <para>
/// <b>20 requests per 60 seconds without an API key</b>, read off the <c>ratelimit-limit</c>
/// response header and verified live. That is an order of magnitude tighter than the wiki's
/// prices API, and a walk over a fifty-member group exceeds it on its own. Register for a key
/// and set <see cref="GielinomicsClientOptions.WiseOldManApiKey"/> if you need more, and give
/// this client its own rate limit budget rather than sharing one with the wiki or Jagex.
/// </para>
/// <para>
/// <b>A descriptive User-Agent is required</b>, exactly as on the wiki: a request sent as
/// <c>curl/8.0</c> is answered with a <c>403</c>. Verified live.
/// </para>
/// <para>
/// Wise Old Man's snapshot history predates anything a new project can start collecting, which
/// is the reason to consume this rather than reimplement it. The reverse is true of prices:
/// nothing here retains the fine-grained price record that
/// <see cref="Prices.IPricesClient"/> exists to accumulate.
/// </para>
/// </remarks>
public interface IWiseOldManClient
{
    /// <summary>
    /// Fetches a tracked player, including their most recent snapshot.
    /// </summary>
    /// <remarks>
    /// Returns null for a 404, which means Wise Old Man has never tracked this name — not that
    /// the account does not exist. The two are different claims and only the hiscores can
    /// settle the second.
    /// </remarks>
    /// <param name="username">Display name. Case-insensitive; spaces are encoded for you.</param>
    /// <param name="cancellationToken">Cancels the request.</param>
    /// <returns>The player, or null when not tracked.</returns>
    Task<WiseOldManPlayer?> GetPlayerAsync(string username, CancellationToken cancellationToken = default);

    /// <summary>
    /// Fetches what a player gained over a rolling window.
    /// </summary>
    /// <remarks>
    /// The returned window is bounded by the snapshots that exist, so it can be much shorter
    /// than the period asked for. Read <see cref="WiseOldManGains.StartsAt"/> and
    /// <see cref="WiseOldManGains.EndsAt"/> before turning a total into a rate.
    /// </remarks>
    /// <param name="username">Display name.</param>
    /// <param name="period">The rolling window.</param>
    /// <param name="cancellationToken">Cancels the request.</param>
    /// <returns>The gains, or null when the player is not tracked.</returns>
    Task<WiseOldManGains?> GetPlayerGainsAsync(
        string username,
        WiseOldManPeriod period = WiseOldManPeriod.Week,
        CancellationToken cancellationToken = default);

    /// <summary>
    /// Fetches a player's snapshots within a window, newest first.
    /// </summary>
    /// <param name="username">Display name.</param>
    /// <param name="period">The window to fetch snapshots from.</param>
    /// <param name="cancellationToken">Cancels the request.</param>
    /// <returns>The snapshots, or null when the player is not tracked.</returns>
    Task<IReadOnlyList<WiseOldManSnapshot>?> GetPlayerSnapshotsAsync(
        string username,
        WiseOldManPeriod period = WiseOldManPeriod.Week,
        CancellationToken cancellationToken = default);

    /// <summary>
    /// Fetches a group and its roster.
    /// </summary>
    /// <param name="groupId">The group identifier.</param>
    /// <param name="cancellationToken">Cancels the request.</param>
    /// <returns>The group, or null when no group has that identifier.</returns>
    Task<WiseOldManGroup?> GetGroupAsync(long groupId, CancellationToken cancellationToken = default);

    /// <summary>
    /// Fetches a group's leaderboard for one metric over a window.
    /// </summary>
    /// <remarks>
    /// One request for the whole roster, which is the point: walking members individually costs
    /// a request each and meets the rate limit at around twenty of them.
    /// </remarks>
    /// <param name="groupId">The group identifier.</param>
    /// <param name="metric">The metric to rank by, e.g. <c>overall</c>, <c>zulrah</c>, <c>ehp</c>.</param>
    /// <param name="period">The rolling window.</param>
    /// <param name="limit">Maximum rows to return, or null for the server's default.</param>
    /// <param name="offset">Rows to skip, for paging.</param>
    /// <param name="cancellationToken">Cancels the request.</param>
    /// <returns>The leaderboard, or null when no group has that identifier.</returns>
    Task<IReadOnlyList<WiseOldManGroupGains>?> GetGroupGainsAsync(
        long groupId,
        string metric = "overall",
        WiseOldManPeriod period = WiseOldManPeriod.Week,
        int? limit = null,
        int? offset = null,
        CancellationToken cancellationToken = default);

    /// <summary>
    /// Fetches a competition and its standings.
    /// </summary>
    /// <param name="competitionId">The competition identifier.</param>
    /// <param name="cancellationToken">Cancels the request.</param>
    /// <returns>The competition, or null when no competition has that identifier.</returns>
    Task<WiseOldManCompetition?> GetCompetitionAsync(long competitionId, CancellationToken cancellationToken = default);

    /// <summary>
    /// Fetches the experience rates efficiency metrics are computed from.
    /// </summary>
    /// <remarks>
    /// Near-static reference data. Cache it for the lifetime of the process rather than
    /// spending a request per use — it changes when Wise Old Man revises its rates, which is
    /// on the order of a few times a year.
    /// </remarks>
    /// <param name="metric">Which rate set: <c>ehp</c> or <c>ehb</c>.</param>
    /// <param name="accountType">Which account type the rates are for, e.g. <c>main</c>, <c>ironman</c>, <c>ultimate</c>.</param>
    /// <param name="cancellationToken">Cancels the request.</param>
    /// <returns>The rates, one entry per skill.</returns>
    Task<IReadOnlyList<WiseOldManEfficiencyRate>> GetEfficiencyRatesAsync(
        string metric = "ehp",
        string accountType = "main",
        CancellationToken cancellationToken = default);
}
