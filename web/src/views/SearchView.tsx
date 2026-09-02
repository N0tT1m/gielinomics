import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { exact, gp } from '../components/charts/format'
import { Card, ErrorNote, Loading } from '../components/ui'
import { useApi } from '../hooks/useApi'
import { useWatchlist } from '../hooks/useWatchlist'

export function SearchView({ onOpenItem }: { readonly onOpenItem: (id: number) => void }) {
  const [term, setTerm] = useState('')
  const [debounced, setDebounced] = useState('')
  const [membersOnly, setMembersOnly] = useState<'' | 'true' | 'false'>('')

  // Debounced: the search hits an ILIKE over every item, and a request per keystroke would
  // spend the API's time answering queries nobody waited for.
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(term.trim()), 250)
    return () => clearTimeout(timer)
  }, [term])

  const results = useApi(
    (signal) =>
      api.searchItems(
        {
          search: debounced || undefined,
          members: membersOnly === '' ? undefined : membersOnly === 'true',
          limit: 50,
        },
        signal,
      ),
    [debounced, membersOnly],
  )

  const watchlist = useWatchlist()

  return (
    <Card title="Items" note="Search this platform's item reference data.">
      <div className="filters">
        <input
          type="search"
          value={term}
          placeholder="Search items…"
          aria-label="Search items"
          onChange={(event) => setTerm(event.target.value)}
          style={{ flex: '1 1 240px' }}
        />
        <select
          value={membersOnly}
          onChange={(event) => setMembersOnly(event.target.value as '' | 'true' | 'false')}
          aria-label="Membership"
        >
          <option value="">All items</option>
          <option value="true">Members</option>
          <option value="false">Free-to-play</option>
        </select>
      </div>

      {results.loading && !results.data ? (
        <Loading what="items" />
      ) : results.error ? (
        <ErrorNote error={results.error} />
      ) : results.data && results.data.items.length > 0 ? (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Item</th>
                <th className="num">Buy limit</th>
                <th className="num">High alch</th>
                <th>Members</th>
                <th aria-label="Watch" />
              </tr>
            </thead>
            <tbody>
              {results.data.items.map((item) => (
                <tr key={item.id}>
                  <td>
                    <button className="link" onClick={() => onOpenItem(item.id)}>
                      {item.name ?? `Item ${item.id}`}
                    </button>
                    {item.isStub && (
                      <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>
                        awaiting mapping
                      </span>
                    )}
                  </td>
                  <td className="num">{item.buyLimit === null ? '—' : exact(item.buyLimit)}</td>
                  <td className="num">{gp(item.highAlch)}</td>
                  <td>{item.members === null ? '—' : item.members ? 'Yes' : 'No'}</td>
                  <td>
                    <button
                      className="ghost"
                      onClick={() => watchlist.toggle({ id: item.id, name: item.name ?? `Item ${item.id}` })}
                      aria-label={watchlist.has(item.id) ? 'Remove from watchlist' : 'Add to watchlist'}
                    >
                      {watchlist.has(item.id) ? '★' : '☆'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="empty">
          {debounced ? `Nothing matches “${debounced}”.` : 'No items yet — the mapping sync has not run.'}
        </p>
      )}
    </Card>
  )
}
