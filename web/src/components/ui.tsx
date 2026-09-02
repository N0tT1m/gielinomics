import type { ReactNode } from 'react'
import { signedPercent } from './charts/format'

export function Card({
  title,
  note,
  actions,
  children,
}: {
  readonly title?: string
  readonly note?: string
  readonly actions?: ReactNode
  readonly children: ReactNode
}) {
  return (
    <section className="card">
      {(title || actions) && (
        <div className="card-head">
          {title && <h2>{title}</h2>}
          {actions}
        </div>
      )}
      {note && <p className="card-note">{note}</p>}
      {children}
    </section>
  )
}

export function StatTile({
  label,
  value,
  sub,
}: {
  readonly label: string
  readonly value: ReactNode
  readonly sub?: ReactNode
}) {
  return (
    <div className="card tile">
      <span className="tile-label">{label}</span>
      <span className="tile-value">{value}</span>
      {sub && <span className="tile-sub">{sub}</span>}
    </div>
  )
}

/**
 * A signed change.
 *
 * The arrow glyph carries the direction and the sign carries it again, so the meaning
 * survives for a reader who cannot separate the red from the green.
 */
export function Delta({ percent }: { readonly percent: number | null | undefined }) {
  if (percent === null || percent === undefined || !Number.isFinite(percent)) {
    return <span className="delta flat">—</span>
  }

  const direction = percent > 0.0001 ? 'up' : percent < -0.0001 ? 'down' : 'flat'
  const glyph = direction === 'up' ? '▲' : direction === 'down' ? '▼' : '–'

  return (
    <span className={`delta ${direction}`}>
      {glyph} {signedPercent(percent)}
    </span>
  )
}

/** Status chip. Always an icon plus a word — never colour alone. */
export function Status({
  level,
  children,
}: {
  readonly level: 'good' | 'warning' | 'critical' | 'unknown'
  readonly children: ReactNode
}) {
  return <span className={`status ${level}`}>{children}</span>
}

export function Meter({ value, label }: { readonly value: number; readonly label: string }) {
  const clamped = Math.max(0, Math.min(1, value))
  return (
    <div
      className="meter"
      role="meter"
      aria-valuenow={Math.round(clamped * 100)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={label}
    >
      <span style={{ width: `${clamped * 100}%` }} />
    </div>
  )
}

export function Segmented<T extends string>({
  options,
  value,
  onChange,
  label,
}: {
  readonly options: readonly { value: T; label: string }[]
  readonly value: T
  readonly onChange: (value: T) => void
  readonly label: string
}) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={option.value === value}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

export function ErrorNote({ error }: { readonly error: Error }) {
  return <p className="error">{error.message}</p>
}

export function Loading({ what }: { readonly what: string }) {
  return <p className="empty">Loading {what}…</p>
}
