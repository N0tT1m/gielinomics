import { useEffect, useState } from 'react'

export type Theme = 'system' | 'light' | 'dark'

const KEY = 'gielinomics.theme'

/**
 * Three-state theme: an explicit choice stamps `data-theme` on the root; "system" stamps
 * nothing and leaves `prefers-color-scheme` to decide.
 */
export function useTheme() {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      const stored = localStorage.getItem(KEY)
      return stored === 'light' || stored === 'dark' ? stored : 'system'
    } catch {
      return 'system'
    }
  })

  useEffect(() => {
    const root = document.documentElement
    if (theme === 'system') root.removeAttribute('data-theme')
    else root.setAttribute('data-theme', theme)

    try {
      localStorage.setItem(KEY, theme)
    } catch {
      // Preference is not persisted. Harmless.
    }
  }, [theme])

  return [theme, setTheme] as const
}
