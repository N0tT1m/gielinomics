using System.Data;
using Dapper;

namespace Gielinomics.Data;

/// <summary>
/// Teaches Dapper to map <c>timestamptz</c> onto <see cref="DateTimeOffset"/>.
/// </summary>
/// <remarks>
/// <para>
/// Npgsql surfaces <c>timestamptz</c> as a UTC <see cref="DateTime"/>. Dapper matches a
/// record's constructor by parameter type before it converts anything, so a read model
/// declaring <see cref="DateTimeOffset"/> fails to materialise at all — at runtime, with a
/// message about a missing constructor rather than about a type mismatch.
/// </para>
/// <para>
/// The models keep <see cref="DateTimeOffset"/> rather than bending to the driver: every
/// timestamp in this system is an absolute instant, and a bare <see cref="DateTime"/> is one
/// careless <c>DateTimeKind</c> away from being silently reinterpreted as local time.
/// </para>
/// </remarks>
public static class DapperTypeHandlers
{
    private static int _registered;

    /// <summary>Registers the handlers. Safe to call more than once.</summary>
    public static void Register()
    {
        if (Interlocked.Exchange(ref _registered, 1) == 1)
        {
            return;
        }

        SqlMapper.AddTypeHandler(new DateTimeOffsetHandler());
    }

    /// <summary>Converts between <see cref="DateTimeOffset"/> and Npgsql's UTC <see cref="DateTime"/>.</summary>
    private sealed class DateTimeOffsetHandler : SqlMapper.TypeHandler<DateTimeOffset>
    {
        /// <inheritdoc />
        public override DateTimeOffset Parse(object value) => value switch
        {
            DateTimeOffset offset => offset,

            // Unspecified is treated as UTC rather than local. Every timestamp column in this
            // schema is timestamptz, so the driver has already normalised the instant; guessing
            // local here would shift it by the host's offset.
            DateTime dateTime => new DateTimeOffset(DateTime.SpecifyKind(dateTime, DateTimeKind.Utc)),

            _ => throw new DataException($"Cannot convert {value?.GetType().Name ?? "null"} to a DateTimeOffset."),
        };

        /// <inheritdoc />
        public override void SetValue(IDbDataParameter parameter, DateTimeOffset value)
        {
            ArgumentNullException.ThrowIfNull(parameter);

            parameter.DbType = DbType.DateTime;
            parameter.Value = value.UtcDateTime;
        }
    }
}
