using System.Diagnostics;
using System.Diagnostics.Metrics;

namespace Gielinomics.Ingest.Infrastructure;

/// <summary>
/// Instrumentation for the ingest workers.
/// </summary>
/// <remarks>
/// Present from the first working commit rather than added later. Poll latency, rows written
/// and gap counts are the numbers that tell you the dataset is healthy, and a 24/7 poller
/// whose only signal is "the process is still up" is exactly the silent-death case the
/// staleness alarm exists to catch.
/// </remarks>
public static class IngestTelemetry
{
    /// <summary>The name both the <see cref="ActivitySource"/> and the <see cref="Meter"/> register under.</summary>
    public const string SourceName = "Gielinomics.Ingest";

    /// <summary>Spans for individual poll attempts.</summary>
    public static ActivitySource ActivitySource { get; } = new(SourceName);

    private static readonly Meter Meter = new(SourceName);

    /// <summary>Poll attempts, tagged by feed and outcome.</summary>
    public static Counter<long> Polls { get; } =
        Meter.CreateCounter<long>("gielinomics.ingest.polls", unit: "{poll}", description: "Poll attempts by feed and outcome.");

    /// <summary>Rows persisted, tagged by feed.</summary>
    public static Counter<long> RowsWritten { get; } =
        Meter.CreateCounter<long>("gielinomics.ingest.rows_written", unit: "{row}", description: "Rows persisted by feed.");

    /// <summary>Missing windows found by a gap sweep, tagged by feed.</summary>
    public static Counter<long> GapsDetected { get; } =
        Meter.CreateCounter<long>("gielinomics.ingest.gaps_detected", unit: "{window}", description: "Missing windows found by a gap sweep.");

    /// <summary>Missing windows successfully backfilled, tagged by feed.</summary>
    public static Counter<long> GapsRepaired { get; } =
        Meter.CreateCounter<long>("gielinomics.ingest.gaps_repaired", unit: "{window}", description: "Missing windows successfully backfilled.");

    /// <summary>Poll wall time, tagged by feed.</summary>
    public static Histogram<double> PollDuration { get; } =
        Meter.CreateHistogram<double>("gielinomics.ingest.poll_duration", unit: "s", description: "Poll wall time by feed.");

    /// <summary>
    /// Seconds since a feed last succeeded, tagged by feed.
    /// </summary>
    /// <remarks>
    /// Published as a gauge by the staleness monitor so Grafana can alert on it directly,
    /// without waiting for the Phase 5 alerting layer.
    /// </remarks>
    public static Gauge<double> StalenessSeconds { get; } =
        Meter.CreateGauge<double>("gielinomics.ingest.staleness", unit: "s", description: "Seconds since a feed last succeeded.");
}
