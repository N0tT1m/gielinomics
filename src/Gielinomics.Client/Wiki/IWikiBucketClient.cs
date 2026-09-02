namespace Gielinomics.Client.Wiki;

/// <summary>
/// Typed access to the OSRS wiki's Bucket API — its structured-data query layer.
/// </summary>
/// <remarks>
/// This is near-static reference data: item stats, drop tables, monster stats. A weekly sync is
/// the most it warrants. It is also what turns retained prices into answers the price API
/// cannot give at all — expected gp per kill, or the best strength bonus per coin.
/// </remarks>
public interface IWikiBucketClient
{
    /// <summary>Runs a query and returns its rows.</summary>
    /// <typeparam name="T">The row type.</typeparam>
    /// <param name="query">The query.</param>
    /// <param name="cancellationToken">Cancels the request.</param>
    /// <returns>The rows, empty when the query matched nothing.</returns>
    Task<IReadOnlyList<T>> QueryAsync<T>(BucketQuery query, CancellationToken cancellationToken = default);

    /// <summary>
    /// Streams an entire bucket, paging by offset.
    /// </summary>
    /// <remarks>
    /// Ordering is required for correctness, not neatness: offset paging over an unordered
    /// result can repeat and skip rows. The caller supplies the field to order by, and the
    /// query builder makes sure it is also selected.
    /// </remarks>
    /// <typeparam name="T">The row type.</typeparam>
    /// <param name="build">Builds a fresh query for each page.</param>
    /// <param name="orderBy">Field to order by.</param>
    /// <param name="pageSize">Rows per request.</param>
    /// <param name="cancellationToken">Cancels the enumeration.</param>
    /// <returns>Every row in the bucket.</returns>
    IAsyncEnumerable<T> StreamAsync<T>(
        Func<BucketQuery> build,
        string orderBy,
        int pageSize = 5000,
        CancellationToken cancellationToken = default);
}
