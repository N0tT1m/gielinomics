import { useState } from 'react'
import { api } from '../api/client'
import { exact, gp } from '../components/charts/format'
import { Card, ErrorNote, Loading, Segmented } from '../components/ui'
import { useApi } from '../hooks/useApi'

const STATS = [
  { value: 'strength', label: 'Strength' },
  { value: 'ranged_strength', label: 'Ranged str' },
  { value: 'prayer', label: 'Prayer' },
  { value: 'stab_attack', label: 'Stab' },
  { value: 'slash_attack', label: 'Slash' },
  { value: 'crush_attack', label: 'Crush' },
  { value: 'magic_attack', label: 'Magic atk' },
  { value: 'range_attack', label: 'Ranged atk' },
  { value: 'stab_defence', label: 'Stab def' },
  { value: 'slash_defence', label: 'Slash def' },
  { value: 'crush_defence', label: 'Crush def' },
  { value: 'magic_defence', label: 'Magic def' },
  { value: 'range_defence', label: 'Ranged def' },
]

const SLOTS = ['', 'weapon', 'head', 'body', 'legs', 'shield', 'hands', 'feet', 'cape', 'neck', 'ring', 'ammo', '2h']

const ORDERS = [
  { value: 'stat', label: 'Best stat' },
  { value: 'value', label: 'Cheapest per point' },
] as const

/**
 * Gear comparison — equipment stats joined to live prices.
 *
 * Neither upstream API can answer this. The wiki knows the bonuses and nothing about what
 * they cost; the prices API knows the cost and nothing about the bonuses.
 */
export function GearView({ onOpenItem }: { readonly onOpenItem: (id: number) => void }) {
  const [stat, setStat] = useState('strength')
  const [slot, setSlot] = useState('')
  const [order, setOrder] = useState<'stat' | 'value'>('stat')
  const [budget, setBudget] = useState('')
  const [includeUntradeable, setIncludeUntradeable] = useState(false)

  const maxPrice = budget === '' ? undefined : Number(budget)

  const gear = useApi(
    (signal) =>
      api.getGear(
        {
          stat,
          slot: slot || undefined,
          maxPrice: Number.isFinite(maxPrice) ? maxPrice : undefined,
          cheapestFirst: order === 'value',
          includeUntradeable,
          limit: 50,
        },
        signal,
      ),
    [stat, slot, order, maxPrice, includeUntradeable],
  )

  return (
    <Card
      title="Gear"
      note="Equipment bonuses from the wiki, priced against retained market history. The biggest bonus is usually just the most expensive one — 'cheapest per point' is the more useful question. Cosmetic and beta variants are hidden by default: identical stats, no price, and they crowd out everything you could actually buy."
    >
      <div className="filters">
        <select value={stat} onChange={(event) => setStat(event.target.value)} aria-label="Stat">
          {STATS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>

        <select value={slot} onChange={(event) => setSlot(event.target.value)} aria-label="Slot">
          {SLOTS.map((option) => (
            <option key={option} value={option}>
              {option === '' ? 'Any slot' : option}
            </option>
          ))}
        </select>

        <input
          type="text"
          inputMode="numeric"
          value={budget}
          placeholder="Max price"
          aria-label="Maximum price"
          onChange={(event) => setBudget(event.target.value.replace(/[^\d]/g, ''))}
          style={{ width: 120 }}
        />

        <Segmented options={ORDERS} value={order} onChange={setOrder} label="Ranking" />

        <label className="muted" style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
          <input
            type="checkbox"
            checked={includeUntradeable}
            onChange={(event) => setIncludeUntradeable(event.target.checked)}
          />
          Include untradeable
        </label>
      </div>

      {gear.loading && !gear.data ? (
        <Loading what="gear" />
      ) : gear.error ? (
        <ErrorNote error={gear.error} />
      ) : gear.data && gear.data.options.length > 0 ? (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Item</th>
                <th>Slot</th>
                <th className="num">Bonus</th>
                <th className="num">Price</th>
                <th className="num">gp per point</th>
              </tr>
            </thead>
            <tbody>
              {gear.data.options.map((option) => (
                <tr key={`${option.pageName}-${option.statValue}-${option.equipmentSlot}`}>
                  <td>
                    {option.itemId === null ? (
                      <span>
                        {option.pageName}
                        <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>
                          untradeable
                        </span>
                      </span>
                    ) : (
                      <button className="link" onClick={() => onOpenItem(option.itemId!)}>
                        {option.name ?? option.pageName}
                      </button>
                    )}
                  </td>
                  <td className="muted">{option.equipmentSlot ?? '—'}</td>
                  <td className="num">
                    <b>{option.statValue}</b>
                  </td>
                  <td className="num">{option.price === null ? '—' : gp(option.price)}</td>
                  <td className="num">{option.gpPerPoint === null ? '—' : exact(Number(option.gpPerPoint))}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="empty">
          Nothing matches. The wiki sync runs weekly — if this is a fresh database it may not have
          run yet.
        </p>
      )}
    </Card>
  )
}
