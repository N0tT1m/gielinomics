using System.Threading.RateLimiting;
using Gielinomics.Alerts;
using Gielinomics.Client;
using Gielinomics.Data;
using Gielinomics.Ingest.Infrastructure;
using Gielinomics.Ingest.Workers;
using OpenTelemetry.Metrics;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;

// A web host for a worker, purely to serve the Prometheus scrape endpoint. Grafana reads
// the ingest metrics directly; there are no application routes here beyond health.
var builder = WebApplication.CreateBuilder(args);

// Blank, not just missing: appsettings.json ships empty placeholders for both, so a
// null check alone lets the empty string through to fail further downstream.
var connectionString = builder.Configuration.GetConnectionString("Gielinomics");
if (string.IsNullOrWhiteSpace(connectionString))
{
    throw new InvalidOperationException(
        "ConnectionStrings__Gielinomics is not configured. Set the ConnectionStrings__Gielinomics "
        + "environment variable, or run 'dotnet user-secrets set ConnectionStrings:Gielinomics \"...\"' "
        + "in src/Gielinomics.Ingest for local development.");
}

var userAgent = builder.Configuration["Gielinomics:UserAgent"];
if (string.IsNullOrWhiteSpace(userAgent))
{
    throw new InvalidOperationException(
        "Gielinomics__UserAgent is not configured. The OSRS wiki blocks default agents.");
}

builder.Services.AddGielinomicsData(connectionString);
builder.Services.AddGielinomicsAlerts();

// One limiter for the wiki, registered as a singleton so every handler instance draws on the
// same budget. Jagex and Wise Old Man get their own the day a client for them exists — a
// shared global limiter would let a backfill against one starve the live poll against another.
// One request per second flat, with almost no burst. The hiscore walk is already spread
// across its polling interval, so it never needs to spend a burst it does not have.
builder.Services.AddKeyedSingleton<RateLimiter>(UpstreamHosts.Jagex, (_, _) =>
    new TokenBucketRateLimiter(new TokenBucketRateLimiterOptions
    {
        TokenLimit = 2,
        TokensPerPeriod = 1,
        ReplenishmentPeriod = TimeSpan.FromSeconds(1),
        QueueLimit = 10_000,
        QueueProcessingOrder = QueueProcessingOrder.OldestFirst,
        AutoReplenishment = true,
    }));

builder.Services.AddKeyedSingleton<RateLimiter>(UpstreamHosts.Wiki, (_, _) =>
    new TokenBucketRateLimiter(new TokenBucketRateLimiterOptions
    {
        // Two per second sustained with a small burst. The live polls need a handful of
        // requests a minute; the burst is headroom for a gap sweep, and the deep queue
        // means the hour-long backfill waits its turn rather than failing.
        TokenLimit = 5,
        TokensPerPeriod = 2,
        ReplenishmentPeriod = TimeSpan.FromSeconds(1),
        QueueLimit = 10_000,
        QueueProcessingOrder = QueueProcessingOrder.OldestFirst,
        AutoReplenishment = true,
    }));

var pricesClient = builder.Services.AddGielinomicsClient(options => options.UserAgent = userAgent);

pricesClient.AddStandardResilienceHandler(options =>
    {
        // Exponential backoff with jitter plus a circuit breaker. Attached to the prices
        // client alone, so a second upstream gets its own budget rather than sharing this
        // one -- Jagex is considerably less forgiving than the wiki.
        options.Retry.MaxRetryAttempts = 4;
        options.Retry.UseJitter = true;
        options.AttemptTimeout.Timeout = TimeSpan.FromSeconds(30);
        options.TotalRequestTimeout.Timeout = TimeSpan.FromMinutes(2);
        options.CircuitBreaker.SamplingDuration = TimeSpan.FromMinutes(1);
    });

// Registered after the resilience handler, so it sits inside it: every retry attempt is paced
// too. Pacing only the first attempt would let a retry storm past the budget entirely.
pricesClient.AddHttpMessageHandler(provider =>
    new RateLimitingHandler(provider.GetRequiredKeyedService<RateLimiter>(UpstreamHosts.Wiki)));

// The wiki's structured-data API shares the wiki's rate limit budget with the prices API --
// same operator, and a weekly bulk sync should not be able to crowd out the 5-minute poll.
var wikiClient = builder.Services.AddGielinomicsWikiClient();

wikiClient.AddStandardResilienceHandler(options =>
{
    // A bucket page is 5000 rows of JSON, so the per-attempt timeout is generous compared to
    // the price feeds'.
    options.Retry.MaxRetryAttempts = 3;
    options.Retry.UseJitter = true;
    options.AttemptTimeout.Timeout = TimeSpan.FromSeconds(60);
    options.TotalRequestTimeout.Timeout = TimeSpan.FromMinutes(4);
    options.CircuitBreaker.SamplingDuration = TimeSpan.FromMinutes(2);
});

wikiClient.AddHttpMessageHandler(provider =>
    new RateLimitingHandler(provider.GetRequiredKeyedService<RateLimiter>(UpstreamHosts.Wiki)));

var hiscoresClient = builder.Services.AddGielinomicsHiscoresClient();

hiscoresClient.AddStandardResilienceHandler(options =>
{
    // Fewer attempts and a twitchier breaker than the wiki gets. Jagex publishes no rate
    // limit, which is not the same as not having one.
    options.Retry.MaxRetryAttempts = 2;
    options.Retry.UseJitter = true;
    options.AttemptTimeout.Timeout = TimeSpan.FromSeconds(20);
    options.TotalRequestTimeout.Timeout = TimeSpan.FromSeconds(90);
    options.CircuitBreaker.SamplingDuration = TimeSpan.FromMinutes(2);
});

hiscoresClient.AddHttpMessageHandler(provider =>
    new RateLimitingHandler(provider.GetRequiredKeyedService<RateLimiter>(UpstreamHosts.Jagex)));

builder.Services.AddOpenTelemetry()
    .ConfigureResource(resource => resource.AddService("gielinomics-ingest"))
    .WithMetrics(metrics => metrics
        .AddMeter(IngestTelemetry.SourceName)
        .AddHttpClientInstrumentation()
        .AddPrometheusExporter())
    .WithTracing(tracing => tracing
        .AddSource(IngestTelemetry.SourceName)
        .AddHttpClientInstrumentation());

// Both aggregate feeds share one worker type, parameterised by cadence and step.
builder.Services.AddSingleton<IHostedService>(sp =>
    ActivatorUtilities.CreateInstance<PriceSeriesWorker>(sp, PriceSeriesWorker.Feed.FiveMinute));
builder.Services.AddSingleton<IHostedService>(sp =>
    ActivatorUtilities.CreateInstance<PriceSeriesWorker>(sp, PriceSeriesWorker.Feed.Hourly));

builder.Services.AddHostedService<LatestPriceWorker>();
builder.Services.AddHostedService<MappingSyncWorker>();

// Walks the tracked-account allowlist. Registered unconditionally: with nothing tracked it
// idles without making a single request.
builder.Services.AddHostedService<HiscoreWorker>();

// Phase 7. Near-static reference data on a weekly cadence; this is what turns retained prices
// into GP-per-kill and gear-per-coin answers.
builder.Services.AddHostedService<BucketSyncWorker>();

// Phase 1 non-negotiable: cannot be added retroactively.
builder.Services.AddHostedService<StalenessMonitor>();

// Phase 5. Lives with the other scheduled jobs rather than in the API, which may be replicated.
builder.Services.AddHostedService<AlertEvaluationWorker>();

// One-shot. Off by default; enable for a single overnight run, then disable.
if (builder.Configuration.GetValue("Gielinomics:RunBackfill", defaultValue: false))
{
    builder.Services.AddHostedService<BackfillWorker>();
}

var app = builder.Build();

app.MapPrometheusScrapingEndpoint();
app.MapGet("/health", () => Results.Ok(new { status = "ok" }));

await app.RunAsync();

/// <summary>Keys identifying an upstream host's rate limit budget.</summary>
internal static class UpstreamHosts
{
    /// <summary>The OSRS wiki, which serves the prices API.</summary>
    public const string Wiki = "wiki";

    /// <summary>Jagex, which serves the official hiscores.</summary>
    public const string Jagex = "jagex";
}
