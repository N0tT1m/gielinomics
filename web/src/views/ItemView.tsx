import { useState } from 'react'
import { api } from '../api/client'
import { PriceChart } from '../components/charts/PriceChart'
import { VolumeChart } from '../components/charts/VolumeChart'
import { exact, gp, percent } from '../components/charts/format'
import { Card, ErrorNote, Loading, Segmented, StatTile } from '../components/ui'
import { useApi } from '../hooks/useApi'
import { useWatchlist } from '../hooks/useWatchlist'

const RANGES = [
  { value: '24h', label: '24h', interval: '5m' },
  { value: '7d', label: '7d', interval: '1h' },
  { value: '30d', label: '30d', interval: '1h' },
  { value: '365d', label: '1y', interval: '1d' },
] as const

type RangeKey = (typeof RANGES)[number]['value']

function since(range: RangeKey): string {
  const days = range === '24h' ? 1 : range === '7d' ? 7 : range === '30d' ? 30 : 365
  return new Date(Date.now() - days * 86_400_000).toISOString()
}

export function ItemView({ itemId, onBack }: { readonly itemId: number; readonly onBack: () => void }) {
  const [range, setRange] = useState<RangeKey>('24h')
  const interval = RANGES.find((entry) => entry.value === range)?.interval ?? '5m'

  const item = useApi((signal) => api.getItem(itemId, signal), [itemId])
  const prices = useApi(
    (signal) => api.getPrices(itemId, { from: since(range), interval }, signal),
    [itemId, range, interval],
  )
  const stats = useApi((signal) => api.getStats(itemId, { window: range, interval }, signal), [itemId, range, interval])

  const watchlist = useWatchlist()
  const name = item.data?.name ?? `Item ${itemId}`

  return (
    <>
      <div className="filters">
        <button className="ghost" onClick={onBack}>
          ← Back
        </button>
        <h1 style={{ marginRight: 'auto' }}>{name}</h1>
        <Segmented options={RANGES} value={range} onChange={setRange} label="Range" />
        <button className="ghost" onClick={() => watchlist.toggle({ id: itemId, name })}>
          {watchlist.has(itemId) ? '★ Watching' : '☆ Watch'}
        </button>
      </div>

      {item.error && <ErrorNote error={item.error} />}

      {item.data && (
        <div className="grid cols-4" style={{ marginBottom: 16 }}>
          <StatTile
            label="Buy limit"
            value={item.data.buyLimit === null ? '—' : exact(item.data.buyLimit)}
            sub={item.data.buyLimit === null ? 'no published limit' : 'per 4 hours'}
          />
          <StatTile label="High alch" value={gp(item.data.highAlch)} sub={`Value ${gp(item.data.value)}`} />
          <StatTile
            label="Volatility"
            value={stats.data?.volatility === null || stats.data === undefined ? '—' : percent(stats.data.volatility * 100)}
            sub="std. deviation over mean"
          />
          <StatTile
            label="Mean spread"
            value={stats.data?.meanSpread === null || stats.data === undefined ? '—' : percent(stats.data.meanSpread * 100)}
            sub={stats.data ? `${exact(stats.data.samples)} bars retained` : undefined}
          />
        </div>
      )}

      <div className="grid" style={{ marginBottom: 16 }}>
        <Card
          title="Price"
          note={`${interval} bars over ${range === '365d' ? 'a year' : range}. Gaps are windows in which nothing traded — the line breaks rather than interpolating across data this platform does not have.`}
        >
          {prices.loading && !prices.data ? (
            <Loading what="price history" />
          ) : prices.error ? (
            <ErrorNote error={prices.error} />
          ) : (
            <>
              <PriceChart points={prices.data?.points ?? []} />
              {(prices.data?.points.length ?? 0) > 0 && (
                <>
                  <h3 style={{ marginTop: 18 }}>Volume</h3>
                  <VolumeChart points={prices.data?.points ?? []} />
                </>
              )}
            </>
          )}
        </Card>
      </div>

      {item.data && (
        <Card title="Details">
          <div className="table-scroll">
            <table>
              <tbody>
                <tr>
                  <th>Examine</th>
                  <td style={{ whiteSpace: 'normal' }}>{item.data.examine ?? '—'}</td>
                </tr>
                <tr>
                  <th>Members</th>
                  <td>{item.data.members ? 'Yes' : 'No'}</td>
                </tr>
                <tr>
                  <th>Low alch</th>
                  <td className="num">{gp(item.data.lowAlch)}</td>
                </tr>
                <tr>
                  <th>Liquidity</th>
                  <td className="num">
                    {stats.data ? `${exact(stats.data.highVolume + stats.data.lowVolume)} units` : '—'}
                  </td>
                </tr>
                <tr>
                  <th>First seen</th>
                  <td>{new Date(item.data.firstSeen).toLocaleString()}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </>
  )
}
