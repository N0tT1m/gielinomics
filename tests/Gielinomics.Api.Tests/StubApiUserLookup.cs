using System.Security.Cryptography;
using System.Text;
using Gielinomics.Data;

namespace Gielinomics.Api.Tests;

/// <summary>An in-memory token table, keyed the way the real one is: by hash.</summary>
internal sealed class StubApiUserLookup : IApiUserLookup
{
    private readonly Dictionary<string, ApiUser> _byHash = new(StringComparer.Ordinal);

    /// <summary>Every hash the filter looked up, in order.</summary>
    public List<string> Lookups { get; } = [];

    /// <summary>Registers a token as belonging to a caller.</summary>
    /// <param name="token">The plaintext token a caller would present.</param>
    /// <param name="user">The caller it resolves to.</param>
    public void Add(string token, ApiUser user) => _byHash[HashOf(token)] = user;

    /// <inheritdoc />
    public Task<ApiUser?> FindByTokenHashAsync(byte[] tokenHash, CancellationToken cancellationToken = default)
    {
        var key = Convert.ToHexString(tokenHash);
        Lookups.Add(key);
        return Task.FromResult(_byHash.GetValueOrDefault(key));
    }

    /// <summary>The hex SHA-256 of a token, as the stub keys itself.</summary>
    /// <param name="token">The plaintext token.</param>
    /// <returns>Uppercase hex of the hash.</returns>
    public static string HashOf(string token)
        => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(token)));
}
