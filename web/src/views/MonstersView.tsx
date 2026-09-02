import { useState } from 'react'
import { api } from '../api/client'
import { exact, gp } from '../components/charts/format'
import { Card, ErrorNote, Loading, StatTile } from '../components/ui'
import { useApi } from '../hooks/useApi'

/**
 * A monster's drop table, priced.
 *
 * The cross-source join the whole design is for: the wiki knows what drops and how often, the
 * retained price history knows what it is worth, and neither upstream API can multiply them.
 */
export function MonstersView({ onOpenItem }: { readonly onOpenItem: (id: number) => void }) {
  const [input, setInput] = useState('')
  const [name, setName] = useState('')

  const drops = useApi(
    (signal) => (name ? api.getMonsterDrops(name, 100, signal) : Promise.resolve(undefined)),
    [name],
  )

  return (
    <>
      <Card
        title="Monsters"
        note="What a kill is worth: each drop's rarity times its mean quantity times its current price."
      >
        <form
          className="filters"
          onSubmit={(event) => {
            event.preventDefault()
            setName(input.trim())
          }}
        >
          <input
            type="search"
            value={input}
            placeholder="Monster name, e.g. Abyssal demon"
            aria-label="Monster name"
            onChange={(event) => setInput(event.target.value)}
            style={{ flex: '1 1 240px' }}
          />
          <button className="ghost" type="submit">
            Look up
          </button>
        </form>

        {!name && <p className="empty">Search for a monster to see its drop table priced against retained history.</p>}

        {name && drops.loading && !drops.data ? (
          <Loading what={name} />
        ) : drops.error ? (
          <ErrorNote error={drops.error} />
        ) : drops.data ? (
          <div className="grid cols-4">
            <StatTile
              label="Expected gp per kill"
              value={gp(Number(drops.data.totalExpectedValue))}
              sub="a floor, not a total"
            />
            <StatTile label="Combat level" value={drops.data.monster?.combatLevel ?? '—'} />
            <StatTile label="Hitpoints" value={drops.data.monster?.hitpoints ?? '—'} />
            <StatTile
              label="Slayer xp"
              value={drops.data.monster?.slayerExperience ?? '—'}
              sub={drops.data.monster?.slayerLevel ? `level ${drops.data.monster.slayerLevel}` : undefined}
            />
          </div>
        ) : null}
      </Card>

      {name && drops.data && (
        <>
          <div style={{ height: 16 }} />
          <Card
            title="Drop table"
            note={
              drops.data.unpricedDrops > 0
                ? `${drops.data.unpricedDrops} of ${drops.data.drops.length} rows contribute nothing to the total — the wiki records their rarity qualitatively, or this platform has no price for them yet. The figure above is therefore a floor.`
                : 'Every row is priced.'
            }
          >
            {drops.data.drops.length === 0 ? (
              <p className="empty">No drop table for that name. The weekly wiki sync may not have run yet.</p>
            ) : (
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Item</th>
                      <th>Rarity</th>
                      <th className="num">Qty</th>
                      <th className="num">Price</th>
                      <th className="num">gp per kill</th>
                    </tr>
                  </thead>
                  <tbody>
                    {drops.data.drops.map((drop, index) => (
                      <tr key={`${drop.itemName}-${drop.rarityText}-${index}`}>
                        <td>
                          {drop.itemId === null ? (
                            <span>
                              {drop.itemName}
                              <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>
                                untradeable
                              </span>
                            </span>
                          ) : (
                            <button className="link" onClick={() => onOpenItem(drop.itemId!)}>
                              {drop.itemName}
                            </button>
                          )}
                          {drop.rareDropTable && (
                            <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>
                              rare drop table
                            </span>
                          )}
                        </td>
                        <td className="muted">{drop.rarityText ?? '—'}</td>
                        <td className="num">
                          {drop.quantityLow === drop.quantityHigh
                            ? (drop.quantityLow ?? '—')
                            : `${drop.quantityLow ?? '?'}–${drop.quantityHigh ?? '?'}`}
                        </td>
                        <td className="num">{drop.unitPrice === null ? '—' : gp(drop.unitPrice)}</td>
                        <td className="num">
                          {drop.expectedValue === null ? (
                            <span className="muted">—</span>
                          ) : (
                            <b>{exact(Number(drop.expectedValue))}</b>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </>
      )}
    </>
  )
}
