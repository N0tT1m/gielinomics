using System.Globalization;
using System.Security.Cryptography;
using System.Text;

namespace Gielinomics.Client.Hiscores;

/// <summary>
/// Decides when two hiscore standings represent the same thing.
/// </summary>
/// <remarks>
/// <para>
/// <b>Rank is deliberately excluded.</b> A player's rank moves whenever anyone else passes
/// them, so it changes continuously for an account that has not logged in for months. Hashing
/// the whole payload therefore produces a fresh hash on essentially every poll, the dedup
/// never fires, and <c>hiscore_snapshots</c> grows by a row per account per hour forever —
/// the exact outcome the dedup exists to prevent, and it is invisible until you watch a real
/// account for an hour.
/// </para>
/// <para>
/// What is hashed is what the player did: levels, experience and activity scores. Rank is
/// still stored, in both the payload and <c>skill_samples</c>, so rank movement remains
/// queryable — it just no longer counts as the player having played.
/// </para>
/// </remarks>
public static class HiscoreContentHash
{
    /// <summary>Hashes the activity-bearing fields of a standing.</summary>
    /// <param name="profile">The standing.</param>
    /// <returns>A SHA-256 hash suitable for the <c>content_hash</c> column.</returns>
    /// <exception cref="ArgumentNullException"><paramref name="profile"/> is null.</exception>
    public static byte[] Compute(HiscoreProfile profile)
    {
        ArgumentNullException.ThrowIfNull(profile);

        var builder = new StringBuilder(1024);

        // Positional and explicitly ordered. A dictionary iteration order change upstream must
        // not read as the player having gained a level.
        foreach (var skill in profile.Skills.OrderBy(skill => skill.Id))
        {
            builder.Append(CultureInfo.InvariantCulture, $"s{skill.Id}:{skill.Level}:{skill.Xp};");
        }

        foreach (var activity in profile.Activities.OrderBy(activity => activity.Id))
        {
            builder.Append(CultureInfo.InvariantCulture, $"a{activity.Id}:{activity.Score};");
        }

        return SHA256.HashData(Encoding.UTF8.GetBytes(builder.ToString()));
    }
}
