using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Gielinomics.Client;
using Gielinomics.Data;
using Gielinomics.Ingest.Workers;

var builder = Host.CreateApplicationBuilder(args);

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

builder.Services
    .AddGielinomicsClient(options => options.UserAgent = userAgent)
    .AddStandardResilienceHandler(options =>
    {
        // Exponential backoff with jitter plus a circuit breaker, per upstream host.
        // TODO: split budgets -- Jagex is less forgiving than the wiki.
        options.Retry.MaxRetryAttempts = 4;
        options.Retry.UseJitter = true;
        options.AttemptTimeout.Timeout = TimeSpan.FromSeconds(30);
        options.TotalRequestTimeout.Timeout = TimeSpan.FromMinutes(2);
        options.CircuitBreaker.SamplingDuration = TimeSpan.FromMinutes(1);
    });

// Both aggregate feeds share one worker type, parameterised by cadence and step.
builder.Services.AddSingleton<IHostedService>(sp =>
    ActivatorUtilities.CreateInstance<PriceSeriesWorker>(sp, PriceSeriesWorker.Feed.FiveMinute));
builder.Services.AddSingleton<IHostedService>(sp =>
    ActivatorUtilities.CreateInstance<PriceSeriesWorker>(sp, PriceSeriesWorker.Feed.Hourly));

builder.Services.AddHostedService<LatestPriceWorker>();
builder.Services.AddHostedService<MappingSyncWorker>();

// Phase 1 non-negotiable: cannot be added retroactively.
builder.Services.AddHostedService<StalenessMonitor>();

// One-shot. Off by default; enable for a single overnight run, then disable.
if (builder.Configuration.GetValue("Gielinomics:RunBackfill", defaultValue: false))
{
    builder.Services.AddHostedService<BackfillWorker>();
}

// TODO: ActivitySource + OpenTelemetry -- poll latency, rows written, gap counts.

await builder.Build().RunAsync();
