using System.Net;
using System.Text.Json;
using Gielinomics.Client.Prices;
using Gielinomics.Data;
using Gielinomics.Ingest.Workers;
using Npgsql;
using Xunit;

namespace Gielinomics.Ingest.Tests;

/// <summary>
/// Failure classification for the ingest audit trail.
/// </summary>
/// <remarks>
/// The recorded outcome is what someone reads at 2am to decide where to look. A parse error
/// filed as an HTTP error sends them to the wiki's status page instead of to the schema
/// change that actually broke things.
/// </remarks>
public class IngestFailureTests
{
    [Fact]
    public void A_wrapped_json_error_is_a_parse_error_not_an_http_error()
    {
        // The client wraps unreadable bodies, so the outer type is the same in both cases.
        // Only the inner exception separates "the shape changed" from "the server is down".
        var exception = new PricesApiException("bad body", new JsonException("unexpected token"))
        {
            StatusCode = HttpStatusCode.OK,
        };

        Assert.Equal(IngestOutcome.ParseError, IngestFailure.Classify(exception));
    }

    [Fact]
    public void A_status_failure_is_an_http_error()
    {
        var exception = new PricesApiException("503") { StatusCode = HttpStatusCode.ServiceUnavailable };

        Assert.Equal(IngestOutcome.HttpError, IngestFailure.Classify(exception));
    }

    [Theory]
    [InlineData(typeof(HttpRequestException), IngestOutcome.HttpError)]
    [InlineData(typeof(TaskCanceledException), IngestOutcome.HttpError)]
    [InlineData(typeof(JsonException), IngestOutcome.ParseError)]
    public void Transport_and_serialisation_failures_are_classified_by_type(Type exceptionType, string expected)
    {
        var exception = (Exception)Activator.CreateInstance(exceptionType)!;

        Assert.Equal(expected, IngestFailure.Classify(exception));
    }

    [Fact]
    public void Database_failures_are_db_errors()
    {
        Assert.Equal(IngestOutcome.DbError, IngestFailure.Classify(new NpgsqlException("connection refused")));
    }

    [Fact]
    public void An_unrecognised_failure_admits_that_it_is_unrecognised()
    {
        // Folding this into db_error would send the reader to the database for a bug that
        // is not there.
        Assert.Equal(IngestOutcome.UnknownError, IngestFailure.Classify(new InvalidOperationException("something new")));
    }

    [Fact]
    public void Describe_includes_the_type_and_the_message()
    {
        var detail = IngestFailure.Describe(new InvalidOperationException("the thing broke"));

        Assert.Contains(nameof(InvalidOperationException), detail, StringComparison.Ordinal);
        Assert.Contains("the thing broke", detail, StringComparison.Ordinal);
    }
}
