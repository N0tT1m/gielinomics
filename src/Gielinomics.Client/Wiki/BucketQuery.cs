using System.Text;

namespace Gielinomics.Client.Wiki;

/// <summary>
/// Builds a Bucket query, which the wiki takes as a Lua expression in a query parameter.
/// </summary>
/// <remarks>
/// <para>
/// Weird Gloop replaced Semantic MediaWiki with their own Bucket extension. <c>action=ask</c>
/// is hard-deprecated — anything written about querying this wiki with SMW or Cargo is out of
/// date.
/// </para>
/// <para>
/// Two rules the API enforces that are easy to trip over: a field used in
/// <see cref="OrderBy"/> must also appear in <see cref="Select"/>, and paging without an order
/// is undefined. Both are handled here rather than left to callers.
/// </para>
/// </remarks>
public sealed class BucketQuery
{
    private readonly string _bucket;
    private readonly List<string> _select = [];
    private readonly List<(string Field, string Value)> _where = [];
    private string? _orderBy;
    private int? _limit;
    private int? _offset;

    /// <summary>Starts a query against a bucket.</summary>
    /// <param name="bucket">Bucket name, such as <c>infobox_item</c>.</param>
    /// <exception cref="ArgumentException"><paramref name="bucket"/> is null or blank.</exception>
    public BucketQuery(string bucket)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(bucket);
        _bucket = bucket;
    }

    /// <summary>Adds fields to the projection.</summary>
    /// <param name="fields">Field names.</param>
    /// <returns>This query, for chaining.</returns>
    public BucketQuery Select(params string[] fields)
    {
        ArgumentNullException.ThrowIfNull(fields);
        _select.AddRange(fields);
        return this;
    }

    /// <summary>Adds an equality filter.</summary>
    /// <param name="field">Field name.</param>
    /// <param name="value">Value to match.</param>
    /// <returns>This query, for chaining.</returns>
    public BucketQuery Where(string field, string value)
    {
        _where.Add((field, value));
        return this;
    }

    /// <summary>
    /// Orders the result.
    /// </summary>
    /// <remarks>
    /// The field is added to the projection if it is not already there — the API rejects an
    /// <c>orderBy</c> on a field that is not selected, and discovering that at sync time rather
    /// than here would mean a failed weekly run.
    /// </remarks>
    /// <param name="field">Field to order by.</param>
    /// <returns>This query, for chaining.</returns>
    public BucketQuery OrderBy(string field)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(field);

        if (!_select.Contains(field, StringComparer.Ordinal))
        {
            _select.Add(field);
        }

        _orderBy = field;
        return this;
    }

    /// <summary>Caps the number of rows returned.</summary>
    /// <param name="limit">Maximum rows. The API serves at least 5000.</param>
    /// <returns>This query, for chaining.</returns>
    public BucketQuery Limit(int limit)
    {
        _limit = limit;
        return this;
    }

    /// <summary>Skips rows, for paging.</summary>
    /// <param name="offset">Rows to skip.</param>
    /// <returns>This query, for chaining.</returns>
    public BucketQuery Offset(int offset)
    {
        _offset = offset;
        return this;
    }

    /// <summary>Renders the Lua expression the API expects.</summary>
    /// <returns>The query string.</returns>
    /// <exception cref="InvalidOperationException">Nothing was selected.</exception>
    public override string ToString()
    {
        if (_select.Count == 0)
        {
            throw new InvalidOperationException("A Bucket query must select at least one field.");
        }

        var builder = new StringBuilder();
        builder.Append("bucket(").Append(Quote(_bucket)).Append(')');

        builder.Append(".select(");
        for (var i = 0; i < _select.Count; i++)
        {
            if (i > 0) builder.Append(',');
            builder.Append(Quote(_select[i]));
        }
        builder.Append(')');

        foreach (var (field, value) in _where)
        {
            builder.Append(".where(").Append(Quote(field)).Append(',').Append(Quote(value)).Append(')');
        }

        if (_orderBy is not null) builder.Append(".orderBy(").Append(Quote(_orderBy)).Append(')');
        if (_limit is { } limit) builder.Append(".limit(").Append(limit).Append(')');
        if (_offset is { } offset) builder.Append(".offset(").Append(offset).Append(')');

        return builder.Append(".run()").ToString();
    }

    /// <summary>
    /// Renders a Lua single-quoted string literal.
    /// </summary>
    /// <remarks>
    /// The value is interpolated into code the wiki evaluates, so the escaping is not cosmetic.
    /// An item name containing an apostrophe — "Ava's accumulator", and there are hundreds —
    /// would otherwise terminate the literal and produce a syntax error at best.
    /// </remarks>
    /// <param name="value">The value.</param>
    /// <returns>The quoted literal.</returns>
    internal static string Quote(string value)
    {
        ArgumentNullException.ThrowIfNull(value);

        var escaped = value
            .Replace("\\", "\\\\", StringComparison.Ordinal)
            .Replace("'", "\\'", StringComparison.Ordinal)
            .Replace("\n", "\\n", StringComparison.Ordinal)
            .Replace("\r", "\\r", StringComparison.Ordinal);

        return $"'{escaped}'";
    }
}
