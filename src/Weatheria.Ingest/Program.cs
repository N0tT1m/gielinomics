using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Weatheria.Client;
using Weatheria.Data;
using Weatheria.Ingest.Workers;

var builder = Host.CreateApplicationBuilder(args);

var connectionString = builder.Configuration.GetConnectionString("Weatheria")
    ?? throw new InvalidOperationException("ConnectionStrings__Weatheria is not configured.");

var userAgent = builder.Configuration["Weatheria:UserAgent"]
    ?? throw new InvalidOperationException(
        "Weatheria__UserAgent is not configured. The OSRS wiki blocks default agents.");

builder.Services.AddWeatheriaData(connectionString);

builder.Services
    .AddWeatheriaClient(options => options.UserAgent = userAgent)
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
if (builder.Configuration.GetValue("Weatheria:RunBackfill", defaultValue: false))
{
    builder.Services.AddHostedService<BackfillWorker>();
}

// TODO: ActivitySource + OpenTelemetry -- poll latency, rows written, gap counts.

await builder.Build().RunAsync();
