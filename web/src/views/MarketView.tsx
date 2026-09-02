import { useState } from 'react'
import { api } from '../api/client'
import { exact, gp } from '../components/charts/format'
import { Card, Delta, ErrorNote, Loading, Segmented } from '../components/ui'
import { useApi } from '../hooks/useApi'

const WINDOWS = [
  { value: '6h', label: '6h' },
  { value: '24h', label: '24h' },
  { value: '7d', label: '7d' },
  { value: '30d', label: '30d' },
] as const

export function MarketView({ onOpenItem }: { readonly onOpenItem: (id: number) => void }) {
  const [window, setWindow] = useState<string>('24h')
  const [minVolume, setMinVolume] = useState(100)

  const movers = useApi((signal) => api.getMovers({ window, minVolume, limit: 25 }, signal), [window, minVolume])
  const spreads = useApi((signal) => api.getSpreads({ minVolume, limit: 25 }, signal), [minVolume])

  return (
    <>
      <div className="filters">
        <Segmented options={WINDOWS} value={window} onChange={setWindow} label="Window" />
        <label className="muted" htmlFor="minVolume">
          Min volume
        </label>
        <select
          id="minVolume"
          value={minVolume}
          onChange={(event) => setMinVolume(Number(event.target.value))}
        >
          <option value={0}>Any</option>
          <option value={100}>100</option>
          <option value={1000}>1,000</option>
          <option value={10000}>10,000</option>
        </select>
      </div>

      <div className="grid cols-2">
        <Card
          title="Movers"
          note={`Largest price change over ${window}, measured against this platform's retained history.`}
        >
          {movers.loading && !movers.data ? (
            <Loading what="movers" />
          ) : movers.error ? (
            <ErrorNote error={movers.error} />
          ) : movers.data && movers.data.movers.length > 0 ? (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Item</th>
                    <th className="num">From</th>
                    <th className="num">To</th>
                    <th className="num">Change</th>
                    <th className="num">Volume</th>
                  </tr>
                </thead>
                <tbody>
                  {movers.data.movers.map((mover) => (
                    <tr key={mover.itemId}>
                      <td>
                        <button className="link" onClick={() => onOpenItem(mover.itemId)}>
                          {mover.name ?? `Item ${mover.itemId}`}
                        </button>
                      </td>
                      <td className="num">{gp(mover.startPrice)}</td>
                      <td className="num">{gp(mover.endPrice)}</td>
                      <td className="num">
                        <Delta percent={mover.changePercent} />
                      </td>
                      <td className="num">{exact(mover.volume)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="empty">
              Nothing has moved enough yet. The worker needs a couple of windows of history before
              this fills in.
            </p>
          )}
        </Card>

        <Card
          title="Margins"
          note="Buy at the instant-sell price, sell at the instant-buy price, after Grand Exchange tax. Ranked by profit per buy limit, not per unit."
        >
          {spreads.loading && !spreads.data ? (
            <Loading what="margins" />
          ) : spreads.error ? (
            <ErrorNote error={spreads.error} />
          ) : spreads.data && spreads.data.candidates.length > 0 ? (
            <>
              {!spreads.data.exemptionsResolved && (
                <p className="card-note">
                  Tax exemptions have not been resolved from the item mapping yet, so exempt items are
                  over-taxed here. Figures are pessimistic, never optimistic.
                </p>
              )}
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Item</th>
                      <th className="num">Buy</th>
                      <th className="num">Sell</th>
                      <th className="num">Tax</th>
                      <th className="num">Net</th>
                      <th className="num">Per limit</th>
                    </tr>
                  </thead>
                  <tbody>
                    {spreads.data.candidates.map((candidate) => (
                      <tr key={candidate.itemId}>
                        <td>
                          <button className="link" onClick={() => onOpenItem(candidate.itemId)}>
                            {candidate.name ?? `Item ${candidate.itemId}`}
                          </button>
                        </td>
                        <td className="num">{gp(candidate.buyPrice)}</td>
                        <td className="num">{gp(candidate.sellPrice)}</td>
                        <td className="num muted">{gp(candidate.tax)}</td>
                        <td className="num">
                          <b>{gp(candidate.netMargin)}</b>
                        </td>
                        <td className="num">{gp(candidate.netMarginPerLimit)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="footnote">
                Tax is {(spreads.data.taxRate * 100).toFixed(0)}% of the sale, capped at{' '}
                {gp(spreads.data.taxCapPerItem)} per item, waived under 50 gp.
              </p>
            </>
          ) : (
            <p className="empty">No margin clears the tax at this volume floor right now.</p>
          )}
        </Card>
      </div>
    </>
  )
}
