using System.Text.Json;
using Gielinomics.Client.Prices;
using Gielinomics.Data;
using Npgsql;

namespace Gielinomics.Ingest.Workers;

/// <summary>Maps an exception to the <see cref="IngestOutcome"/> that describes it.</summary>
internal static class IngestFailure
{
    /// <summary>Classifies a failed poll.</summary>
    /// <param name="exception">What went wrong.</param>
    /// <returns>One of the <see cref="IngestOutcome"/> values.</returns>
    public static string Classify(Exception exception) => exception switch
    {
        // The client wraps an unreadable body in PricesApiException, so the inner exception
        // is what separates "the wiki changed its shape" from "the wiki returned a 503".
        PricesApiException { InnerException: JsonException } => IngestOutcome.ParseError,
        PricesApiException => IngestOutcome.HttpError,
        HttpRequestException => IngestOutcome.HttpError,
        TaskCanceledException => IngestOutcome.HttpError,
        JsonException => IngestOutcome.ParseError,
        NpgsqlException => IngestOutcome.DbError,
        _ => IngestOutcome.UnknownError,
    };

    /// <summary>Renders an exception for the audit trail's detail column.</summary>
    /// <param name="exception">What went wrong.</param>
    /// <returns>Type name and message.</returns>
    public static string Describe(Exception exception)
        => $"{exception.GetType().Name}: {exception.Message}";
}
