using Gielinomics.Alerts;
using Gielinomics.Api.Endpoints;
using Gielinomics.Api.Infrastructure;
using Gielinomics.Client;
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

// The API needs the tax rules for the margin scan, and the refresher that keeps their exempt
// set current. It does not evaluate alert rules — that lives in the worker, so a replicated
// API cannot fire the same rule once per replica.
builder.Services.AddGielinomicsAlerts();

// Only for account type detection on the track route. The API polls nothing itself; that is
// the worker's job.
builder.Services.AddGielinomicsClient(options =>
    options.UserAgent = builder.Configuration["Gielinomics:UserAgent"]
        ?? throw new InvalidOperationException(
            "Gielinomics__UserAgent is not configured. Jagex and the wiki both block default agents."));
builder.Services.AddGielinomicsHiscoresClient();

builder.Services.AddOpenApi();
builder.Services.AddProblemDetails();

var app = builder.Build();

app.UseExceptionHandler();
app.UseStatusCodePages();

app.MapOpenApi();
app.MapGet("/health", () => Results.Ok(new HealthResponse("ok"))).Produces<HealthResponse>();

app.MapItemEndpoints();
app.MapMarketEndpoints();
app.MapIngestEndpoints();
app.MapAlertEndpoints();
app.MapPlayerEndpoints();

// -----------------------------------------------------------------------------
// Not mapped: the /api/players/* routes from plan.md.
//
// They depend on hiscore polling, which plan.md's own scope decision recommends cutting
// from v1 — that surface is Wise Old Man, whose snapshot history predates anything this
// project could start collecting. The tables remain in the schema so the decision stays
// reversible, but shipping routes with no ingest behind them would mean endpoints that
// can only ever answer 404.
// -----------------------------------------------------------------------------

await app.RunAsync();
