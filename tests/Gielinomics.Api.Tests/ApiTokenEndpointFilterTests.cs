using Gielinomics.Api.Infrastructure;
using Gielinomics.Data;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Http.HttpResults;
using Xunit;

namespace Gielinomics.Api.Tests;

/// <summary>
/// The token check on the two write routes.
/// </summary>
/// <remarks>
/// Tracking adds unbounded polling load and an alert rule hands the server an outbound URL,
/// so "the filter let the request through" is the failure that matters. Every test here
/// asserts on whether <c>next</c> ran, not only on the status code.
/// </remarks>
public sealed class ApiTokenEndpointFilterTests
{
    private const string Token = "0123456789abcdef";

    private static readonly ApiUser Caller = new(7, "integration");

    [Fact]
    public async Task Runs_the_endpoint_when_the_token_is_known()
    {
        var (filter, http, next, users) = Build($"Bearer {Token}");

        var result = await filter.InvokeAsync(EndpointFilterInvocationContext.Create(http), next.Delegate);

        Assert.True(next.Ran);
        Assert.Equal(Next.Sentinel, result);
    }

    [Fact]
    public async Task Stashes_the_caller_for_the_endpoint_to_read()
    {
        var (filter, http, next, users) = Build($"Bearer {Token}");

        await filter.InvokeAsync(EndpointFilterInvocationContext.Create(http), next.Delegate);

        Assert.Equal(Caller, ApiTokenEndpointFilter.GetUser(http));
    }

    [Fact]
    public async Task Looks_the_token_up_by_hash_and_never_by_value()
    {
        // The table stores hashes; a dump of api_users must not hand anyone a credential.
        var (filter, http, next, users) = Build($"Bearer {Token}");

        await filter.InvokeAsync(EndpointFilterInvocationContext.Create(http), next.Delegate);

        Assert.Equal([StubApiUserLookup.HashOf(Token)], users.Lookups);
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("Bearer")]
    [InlineData("Bearer ")]
    [InlineData("Bearer    ")]
    [InlineData("Basic dXNlcjpwYXNz")]
    [InlineData("Token 0123456789abcdef")]
    [InlineData("0123456789abcdef")]
    public async Task Rejects_a_request_without_a_usable_bearer_token(string? header)
    {
        var (filter, http, next, users) = Build(header);

        var result = await filter.InvokeAsync(EndpointFilterInvocationContext.Create(http), next.Delegate);

        Assert.False(next.Ran);
        AssertChallenged(http, result);
    }

    [Fact]
    public async Task Rejects_an_unrecognised_token()
    {
        var (filter, http, next, users) = Build("Bearer not-the-right-token");

        var result = await filter.InvokeAsync(EndpointFilterInvocationContext.Create(http), next.Delegate);

        Assert.False(next.Ran);
        AssertChallenged(http, result);
    }

    [Fact]
    public async Task Rejects_a_token_differing_only_in_case()
    {
        // The hash is over the exact bytes. A case-insensitive match anywhere in this path
        // would shrink the keyspace of a 32-byte hex token by half its entropy.
        var (filter, http, next, users) = Build($"Bearer {Token.ToUpperInvariant()}");

        await filter.InvokeAsync(EndpointFilterInvocationContext.Create(http), next.Delegate);

        Assert.False(next.Ran);
    }

    [Fact]
    public async Task Accepts_the_scheme_case_insensitively()
    {
        // RFC 7235 makes the scheme token case-insensitive; the credential is not.
        var (filter, http, next, users) = Build($"bearer {Token}");

        await filter.InvokeAsync(EndpointFilterInvocationContext.Create(http), next.Delegate);

        Assert.True(next.Ran);
    }

    [Fact]
    public async Task Tolerates_surrounding_whitespace_in_the_credential()
    {
        var (filter, http, next, users) = Build($"Bearer  {Token}  ");

        await filter.InvokeAsync(EndpointFilterInvocationContext.Create(http), next.Delegate);

        Assert.True(next.Ran);
    }

    [Fact]
    public async Task Says_the_same_thing_for_a_missing_and_a_wrong_token()
    {
        // Distinguishing them turns the endpoint into an oracle for whether a guess exists.
        var (missingFilter, missingHttp, missingNext, _) = Build(header: null);
        var (wrongFilter, wrongHttp, wrongNext, _) = Build("Bearer wrong");

        var missing = await missingFilter.InvokeAsync(
            EndpointFilterInvocationContext.Create(missingHttp), missingNext.Delegate);
        var wrong = await wrongFilter.InvokeAsync(
            EndpointFilterInvocationContext.Create(wrongHttp), wrongNext.Delegate);

        var missingProblem = Assert.IsType<ProblemHttpResult>(missing);
        var wrongProblem = Assert.IsType<ProblemHttpResult>(wrong);

        Assert.Equal(missingProblem.StatusCode, wrongProblem.StatusCode);
        Assert.Equal(missingProblem.ProblemDetails.Title, wrongProblem.ProblemDetails.Title);
        Assert.Equal(missingProblem.ProblemDetails.Detail, wrongProblem.ProblemDetails.Detail);
    }

    [Fact]
    public void GetUser_throws_when_the_route_was_not_behind_the_filter()
    {
        // A wiring mistake that let an endpoint read a caller that was never authenticated
        // would be silent otherwise.
        Assert.Throws<InvalidOperationException>(() => ApiTokenEndpointFilter.GetUser(new DefaultHttpContext()));
    }

    /// <summary>Asserts the request was refused with an indistinguishable 401.</summary>
    /// <param name="http">The request.</param>
    /// <param name="result">What the filter returned.</param>
    private static void AssertChallenged(HttpContext http, object? result)
    {
        var problem = Assert.IsType<ProblemHttpResult>(result);
        Assert.Equal(StatusCodes.Status401Unauthorized, problem.StatusCode);
        Assert.Equal("Bearer", http.Response.Headers.WWWAuthenticate.ToString());
    }

    /// <summary>Builds a filter with one known token, plus a request carrying the given header.</summary>
    /// <param name="header">The Authorization header value, or null for none.</param>
    /// <returns>The filter, the request, the endpoint that should or should not run, and the token table.</returns>
    private static (ApiTokenEndpointFilter Filter, HttpContext Http, Next Next, StubApiUserLookup Users) Build(string? header)
    {
        var users = new StubApiUserLookup();
        users.Add(Token, Caller);

        var http = new DefaultHttpContext();
        if (header is not null)
        {
            http.Request.Headers.Authorization = header;
        }

        return (new ApiTokenEndpointFilter(users), http, new Next(), users);
    }

    /// <summary>The endpoint the filter either does or does not reach.</summary>
    private sealed class Next
    {
        /// <summary>What a reached endpoint returns.</summary>
        public const string Sentinel = "endpoint ran";

        /// <summary>Whether the endpoint was reached.</summary>
        public bool Ran { get; private set; }

        /// <summary>The delegate to hand the filter.</summary>
        public EndpointFilterDelegate Delegate => _ =>
        {
            Ran = true;
            return ValueTask.FromResult<object?>(Sentinel);
        };
    }
}
