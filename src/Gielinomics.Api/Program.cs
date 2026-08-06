using Gielinomics.Data;

var builder = WebApplication.CreateBuilder(args);

// Blank, not just missing: appsettings.json ships an empty placeholder, so a null
// check alone lets the empty string through to fail deeper in AddGielinomicsData.
var connectionString = builder.Configuration.GetConnectionString("Gielinomics");
if (string.IsNullOrWhiteSpace(connectionString))
{
    throw new InvalidOperationException(
        "ConnectionStrings__Gielinomics is not configured. Set the ConnectionStrings__Gielinomics "
        + "environment variable, or run 'dotnet user-secrets set ConnectionStrings:Gielinomics \"...\"' "
        + "in src/Gielinomics.Api for local development.");
}

builder.Services.AddGielinomicsData(connectionString);
builder.Services.AddOpenApi();

var app = builder.Build();

app.MapOpenApi();
app.MapGet("/health", () => Results.Ok(new { status = "ok" }));

// -----------------------------------------------------------------------------
// Phase 4 surface, from plan.md. Nothing here ships before there is history to serve.
//
//   GET  /api/ingest/status                          per-feed last success, recent failures
//   GET  /api/ingest/coverage                        fraction of expected windows present
//   GET  /api/items                                  search, filter by members/limit
//   GET  /api/items/{id}
//   GET  /api/items/{id}/prices?from=&to=&interval=
//   GET  /api/items/{id}/stats                       volatility, mean spread, liquidity
//   GET  /api/market/movers?window=24h
//   GET  /api/market/spreads?minVolume=              tax-adjusted via GrandExchangeTaxRules
//   GET  /api/players/{name}                         resolve through player_names
//   GET  /api/players/{name}/history?skill=&from=
//   POST /api/players/{name}/track                   AUTHENTICATED
//   GET  /api/alerts
//   POST /api/alerts                                 AUTHENTICATED + WebhookUrlValidator
//
// The POSTs must not ship before token auth exists: /track admits unbounded polling load,
// /alerts hands the server an arbitrary outbound request target.
//
// Cursor pagination, Cache-Control on hot reads, ETag on item metadata.
// -----------------------------------------------------------------------------

app.Run();
