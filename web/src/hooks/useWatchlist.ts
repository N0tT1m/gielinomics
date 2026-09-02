import { useCallback, useEffect, useState } from 'react'

const KEY = 'gielinomics.watchlist'

export interface WatchedItem {
  readonly id: number
  readonly name: string
}

function read(): WatchedItem[] {
  // Wrapped: a private window, cleared site data, or a browser blocking storage all make this
  // throw or return junk, and none of those should take the page down.
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter(
      (entry): entry is WatchedItem =>
        typeof entry === 'object' && entry !== null && typeof (entry as WatchedItem).id === 'number',
    )
  } catch {
    return []
  }
}

/**
 * A per-viewer watchlist in browser storage.
 *
 * Deliberately local. A watchlist is a convenience for whoever is at this browser, not shared
 * state — persisting it server-side would need accounts this platform does not have.
 */
export function useWatchlist() {
  const [items, setItems] = useState<WatchedItem[]>(read)

  useEffect(() => {
    try {
      localStorage.setItem(KEY, JSON.stringify(items))
    } catch {
      // Storage unavailable. The list still works for this session.
    }
  }, [items])

  const toggle = useCallback((item: WatchedItem) => {
    setItems((current) =>
      current.some((entry) => entry.id === item.id)
        ? current.filter((entry) => entry.id !== item.id)
        : [...current, item],
    )
  }, [])

  const has = useCallback((id: number) => items.some((entry) => entry.id === id), [items])

  return { items, toggle, has, clear: () => setItems([]) }
}
