import { useState } from 'react'
import { api } from '../api/client'
import { duration, durationMs, exact, percent } from '../components/charts/format'
import { Card, ErrorNote, Loading, Meter, Segmented, StatTile, Status } from '../components/ui'
import { useApi } from '../hooks/useApi'

const INTERVALS = [
  { value: '5m', label: '5m' },
  { value: '1h', label: '1h' },
  { value: '1d', label: '1d' },
] as const

/**
 * Staleness thresholds, mirroring the worker's own StalenessMonitor.
 *
 * Duplicated deliberately rather than fetched: this page is the thing you open when the API
 * is the only part still answering, and it should be able to say a feed is dead without
 * needing another endpoint to be alive.
 */
const THRESHOLDS: Record<string, number> = {
  '5m': 15 * 60_000,
  latest: 5 * 60_000,
  '1h': 3 * 3_600_000,
  mapping: 36 * 3_600_000,
  hiscore: 3 * 3_600_000,
}

function level(source: string, sinceLastSuccess: string | null): 'good' | 'warning' | 'critical' | 'unknown' {
  if (sinceLastSuccess === null) return 'unknown'
  const age = durationMs(sinceLastSuccess)
  const threshold = THRESHOLDS[source]
  if (age === null || threshold === undefined) return 'unknown'
  if (age > threshold) return 'critical'
  if (age > threshold * 0.6) return 'warning'
  return 'good'
}

export function HealthView() {
  const [interval, setInterval] = useState<string>('5m')

  const status = useApi((signal) => api.getIngestStatus(signal), [])
  const coverage = useApi((signal) => api.getCoverage({ interval, window: '7d' }, signal), [interval])

  return (
    <>
      <Card
        title="Coverage"
        note="The fraction of expected windows this platform actually holds. A history nobody can audit is not a moat."
        actions={<Segmented options={INTERVALS} value={interval} onChange={setInterval} label="Granularity" />}
      >
        {coverage.loading && !coverage.data ? (
          <Loading what="coverage" />
        ) : coverage.error ? (
          <ErrorNote error={coverage.error} />
        ) : coverage.data ? (
          <>
            <div className="grid cols-4" style={{ marginBottom: 14 }}>
              <StatTile
                label="Coverage"
                value={percent(coverage.data.coverage * 100)}
                sub="of the last 7 days"
              />
              <StatTile label="Windows held" value={exact(coverage.data.presentWindows)} />
              <StatTile label="Windows expected" value={exact(coverage.data.expectedWindows)} />
              <StatTile
                label="Missing"
                value={exact(coverage.data.expectedWindows - coverage.data.presentWindows)}
                sub="gap repair fills these"
              />
            </div>
            <Meter value={coverage.data.coverage} label={`${interval} coverage over the last 7 days`} />
          </>
        ) : null}
      </Card>

      <div style={{ height: 16 }} />

      <Card
        title="Feeds"
        note="Written on every poll attempt, successful or not — the only thing distinguishing a quiet market from a dead worker."
      >
        {status.loading && !status.data ? (
          <Loading what="feed status" />
        ) : status.error ? (
          <ErrorNote error={status.error} />
        ) : status.data && status.data.feeds.length > 0 ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Feed</th>
                  <th>State</th>
                  <th className="num">Last success</th>
                  <th className="num">Runs (24h)</th>
                  <th className="num">Failures (24h)</th>
                </tr>
              </thead>
              <tbody>
                {status.data.feeds.map((feed) => {
                  const state = level(feed.source, feed.sinceLastSuccess)
                  return (
                    <tr key={feed.source}>
                      <td>
                        <b>{feed.source}</b>
                      </td>
                      <td>
                        <Status level={state}>
                          {state === 'good'
                            ? 'Healthy'
                            : state === 'warning'
                              ? 'Slowing'
                              : state === 'critical'
                                ? 'Stale'
                                : 'Unknown'}
                        </Status>
                      </td>
                      <td className="num">{duration(feed.sinceLastSuccess)} ago</td>
                      <td className="num">{exact(feed.runsLastDay)}</td>
                      <td className="num">
                        {feed.failuresLastDay > 0 ? <b>{exact(feed.failuresLastDay)}</b> : '0'}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="empty">No polls recorded yet. Start the ingest worker.</p>
        )}
      </Card>
    </>
  )
}
