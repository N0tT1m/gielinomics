import { useState } from 'react'
import { HealthView } from './views/HealthView'
import { ItemView } from './views/ItemView'
import { MarketView } from './views/MarketView'
import { PlayersView } from './views/PlayersView'
import { SearchView } from './views/SearchView'
import { WatchlistView } from './views/WatchlistView'
import { useTheme } from './hooks/useTheme'

type Tab = 'market' | 'items' | 'watchlist' | 'players' | 'health'

const TABS: readonly { id: Tab; label: string }[] = [
  { id: 'market', label: 'Market' },
  { id: 'items', label: 'Items' },
  { id: 'watchlist', label: 'Watchlist' },
  { id: 'players', label: 'Accounts' },
  { id: 'health', label: 'Health' },
]

export function App() {
  const [tab, setTab] = useState<Tab>('market')
  const [itemId, setItemId] = useState<number | null>(null)
  const [theme, setTheme] = useTheme()

  function openItem(id: number) {
    setItemId(id)
  }

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          Gielinomics<span>OSRS market history</span>
        </div>

        <nav className="nav" aria-label="Sections">
          {TABS.map((entry) => (
            <button
              key={entry.id}
              aria-current={tab === entry.id && itemId === null ? 'page' : undefined}
              onClick={() => {
                setTab(entry.id)
                setItemId(null)
              }}
            >
              {entry.label}
            </button>
          ))}
        </nav>

        <div className="spacer" />

        <button
          className="ghost"
          onClick={() => setTheme(theme === 'dark' ? 'light' : theme === 'light' ? 'system' : 'dark')}
          aria-label={`Theme: ${theme}. Click to change.`}
          title={`Theme: ${theme}`}
        >
          {theme === 'dark' ? '🌙 Dark' : theme === 'light' ? '☀️ Light' : '🖥 System'}
        </button>
      </header>

      <main>
        {itemId !== null ? (
          <ItemView itemId={itemId} onBack={() => setItemId(null)} />
        ) : tab === 'market' ? (
          <MarketView onOpenItem={openItem} />
        ) : tab === 'items' ? (
          <SearchView onOpenItem={openItem} />
        ) : tab === 'watchlist' ? (
          <WatchlistView onOpenItem={openItem} />
        ) : tab === 'players' ? (
          <PlayersView />
        ) : (
          <HealthView />
        )}

        <p className="footnote">
          Price data from the{' '}
          <a href="https://oldschool.runescape.wiki/w/RuneScape:Real-time_Prices" target="_blank" rel="noreferrer">
            OSRS Wiki real-time prices API
          </a>
          , licensed CC BY-NC-SA 3.0. Not affiliated with Jagex.
        </p>
      </main>
    </div>
  )
}
