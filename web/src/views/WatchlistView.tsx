import { api } from '../api/client'
import { Sparkline } from '../components/charts/Sparkline'
import { exact, gp } from '../components/charts/format'
import { Card, Delta, ErrorNote, Loading } from '../components/ui'
import { useApi } from '../hooks/useApi'
import { useWatchlist } from '../hooks/useWatchlist'

export function WatchlistView({ onOpenItem }: { readonly onOpenItem: (id: number) => void }) {
  const watchlist = useWatchlist()
  const ids = watchlist.items.map((item) => item.id)
  const key = ids.join(',')

  const series = useApi(
    async (signal) => {
      // One request per watched item rather than a batch endpoint. The list is a handful of
      // items by nature, and every response is individually cacheable this way.
      const results = await Promise.all(
        ids.map(async (id) => {
          const prices = await api.getPrices(id, { from: new Date(Date.now() - 86_400_000).toISOString(), interval: '5m' }, signal)
          return [id, prices] as const
        }),
      )
      return Object.fromEntries(results)
    },
    [key],
  )

  if (watchlist.items.length === 0) {
    return (
      <Card title="Watchlist">
        <p className="empty">
          Nothing watched yet. Star an item from Items or a market table and it will appear here.
          <br />
          <span className="muted">The list lives in this browser only.</span>
        </p>
      </Card>
    )
  }

  return (
    <Card
      title="Watchlist"
      note="Last 24 hours, 5-minute bars."
      actions={
        <button className="ghost" onClick={watchlist.clear}>
          Clear all
        </button>
      }
    >
      {series.loading && !series.data ? (
        <Loading what="watched items" />
      ) : series.error ? (
        <ErrorNote error={series.error} />
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Item</th>
                <th>Trend</th>
                <th className="num">Latest buy</th>
                <th className="num">Latest sell</th>
                <th className="num">24h change</th>
                <th className="num">Volume</th>
                <th aria-label="Remove" />
              </tr>
            </thead>
            <tbody>
              {watchlist.items.map((watched) => {
                const points = series.data?.[watched.id]?.points ?? []
                const highs = points.map((point) => point.avgHigh)
                const withValues = highs.filter((value): value is number => value !== null)
                const firstValue = withValues[0]
                const lastValue = withValues[withValues.length - 1]
                const latest = [...points].reverse().find((point) => point.avgHigh !== null)
                const change =
                  firstValue !== undefined && lastValue !== undefined && firstValue !== 0
                    ? ((lastValue - firstValue) / firstValue) * 100
                    : null

                return (
                  <tr key={watched.id}>
                    <td>
                      <button className="link" onClick={() => onOpenItem(watched.id)}>
                        {watched.name}
                      </button>
                    </td>
                    <td>
                      <Sparkline values={highs} label={`${watched.name} instant-buy trend`} />
                    </td>
                    <td className="num">{gp(latest?.avgHigh)}</td>
                    <td className="num">{gp(latest?.avgLow)}</td>
                    <td className="num">
                      <Delta percent={change} />
                    </td>
                    <td className="num">
                      {exact(points.reduce((total, point) => total + point.highVolume + point.lowVolume, 0))}
                    </td>
                    <td>
                      <button
                        className="ghost"
                        onClick={() => watchlist.toggle(watched)}
                        aria-label={`Remove ${watched.name} from watchlist`}
                      >
                        ×
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}
