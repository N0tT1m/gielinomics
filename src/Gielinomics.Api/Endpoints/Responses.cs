using Gielinomics.Data;

namespace Gielinomics.Api.Endpoints;

/// <summary>
/// Named shapes for every response body.
/// </summary>
/// <remarks>
/// Named records rather than anonymous objects, because the OpenAPI document is the frontend's
/// type source. An anonymous object produces an endpoint the schema describes as returning
/// nothing at all, and a generated client that types every response as <c>unknown</c> — which
/// is worse than no generation, since it looks like it worked.
/// </remarks>
internal static class Responses
{
    // Marker type only; the records below carry the contract.
}

/// <summary>Liveness.</summary>
/// <param name="Status">Always <c>ok</c> when the process is serving.</param>
public sealed record HealthResponse(string Status);

/// <summary>An item's retained price history.</summary>
/// <param name="ItemId">Item game ID.</param>
/// <param name="StepSeconds">Granularity of the bars.</param>
/// <param name="From">Start of the returned range.</param>
/// <param name="To">End of the returned range.</param>
/// <param name="Points">The bars, oldest first.</param>
public sealed record ItemPriceSeries(
    int ItemId,
    int StepSeconds,
    DateTimeOffset From,
    DateTimeOffset To,
    IReadOnlyList<PricePoint> Points);

/// <summary>Items ranked by price movement.</summary>
/// <param name="Window">The window measured over.</param>
/// <param name="StepSeconds">Granularity measured at.</param>
/// <param name="Movers">The ranking, largest absolute move first.</param>
public sealed record MoversResponse(TimeSpan Window, int StepSeconds, IReadOnlyList<MarketMover> Movers);

/// <summary>A tax-adjusted margin scan.</summary>
/// <param name="TaxRate">Sale tax rate applied.</param>
/// <param name="TaxCapPerItem">Maximum tax per item.</param>
/// <param name="ExemptionsResolved">
/// False until the exempt set has been resolved from the item mapping, meaning exempt items are
/// currently over-taxed in these figures. Surfaced so a caller can tell an over-charged estimate
/// from a correct one.
/// </param>
/// <param name="Candidates">The candidates, best profit per buy limit first.</param>
public sealed record SpreadScan(
    decimal TaxRate,
    long TaxCapPerItem,
    bool ExemptionsResolved,
    IReadOnlyList<MarginCandidate> Candidates);

/// <summary>Per-feed ingest health.</summary>
/// <param name="Feeds">One entry per feed that has ever run.</param>
public sealed record IngestStatusResponse(IReadOnlyList<FeedStatus> Feeds);

/// <summary>A caller's alert rules.</summary>
/// <param name="Rules">The rules, newest first.</param>
public sealed record AlertListResponse(IReadOnlyList<AlertRule> Rules);

/// <summary>A tracked account and every name it has used.</summary>
/// <param name="Player">The account.</param>
/// <param name="Names">Name history, newest first.</param>
public sealed record PlayerResponse(Player Player, IReadOnlyList<PlayerName> Names);

/// <summary>Per-skill history for an account.</summary>
/// <param name="Player">Current display name.</param>
/// <param name="MappingVersion">Which index-to-name mapping the skill indices decode under.</param>
/// <param name="SkillNames">That mapping's names, in index order, so a client never guesses.</param>
/// <param name="Samples">The samples, oldest first.</param>
public sealed record PlayerHistoryResponse(
    string Player,
    int MappingVersion,
    IReadOnlyList<string> SkillNames,
    IReadOnlyList<SkillSample> Samples);

/// <summary>Movement over a period.</summary>
/// <param name="Player">Current display name.</param>
/// <param name="Period">The window measured over.</param>
/// <param name="Overall">
/// The Overall pseudo-skill, reported separately: it is the sum of the rest, so it would always
/// top a ranking by gain and say nothing.
/// </param>
/// <param name="Skills">The real skills, largest gain first.</param>
public sealed record PlayerGainsResponse(
    string Player,
    TimeSpan Period,
    SkillGain? Overall,
    IReadOnlyList<SkillGain> Skills);
