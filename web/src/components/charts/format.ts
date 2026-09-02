/** Compact gp, the way a player reads prices: 1.2m, 823k, 480. */
export function gp(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'

  const sign = value < 0 ? '-' : ''
  const magnitude = Math.abs(value)

  if (magnitude >= 1_000_000_000) return `${sign}${trim(magnitude / 1_000_000_000)}b`
  if (magnitude >= 1_000_000) return `${sign}${trim(magnitude / 1_000_000)}m`
  if (magnitude >= 1_000) return `${sign}${trim(magnitude / 1_000)}k`
  return `${sign}${Math.round(magnitude)}`
}

function trim(value: number): string {
  return value >= 100 ? value.toFixed(0) : value >= 10 ? value.toFixed(1) : value.toFixed(2)
}

/** Full precision with separators, for tooltips and tables where the exact figure matters. */
export function exact(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return Math.round(value).toLocaleString()
}

export function percent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${value.toFixed(digits)}%`
}

export function signedPercent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${value > 0 ? '+' : ''}${value.toFixed(digits)}%`
}

/** Axis-appropriate time label: hours within a day, dates beyond one. */
export function timeLabel(iso: string, spanMs: number): string {
  const date = new Date(iso)
  if (spanMs <= 36 * 3600 * 1000) {
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  }
  return date.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

export function fullTime(iso: string): string {
  return new Date(iso).toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/**
 * Renders a .NET TimeSpan ("00:04:31.2", "1.00:00:00") as something readable.
 *
 * The API serialises TimeSpan in .NET's own format rather than ISO 8601 duration, so this
 * parses that shape rather than reaching for Date.
 */
export function duration(value: string | null | undefined): string {
  if (!value) return 'never'

  const match = /^(?:(\d+)\.)?(\d+):(\d+):(\d+)/.exec(value)
  if (!match) return value

  const [, days, hours, minutes] = match
  const d = Number(days ?? 0)
  const h = Number(hours ?? 0)
  const m = Number(minutes ?? 0)

  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}

/** TimeSpan to milliseconds, for threshold comparisons. */
export function durationMs(value: string | null | undefined): number | null {
  if (!value) return null
  const match = /^(?:(\d+)\.)?(\d+):(\d+):(\d+(?:\.\d+)?)/.exec(value)
  if (!match) return null
  const [, days, hours, minutes, seconds] = match
  return (
    Number(days ?? 0) * 86_400_000 +
    Number(hours ?? 0) * 3_600_000 +
    Number(minutes ?? 0) * 60_000 +
    Number(seconds ?? 0) * 1000
  )
}
