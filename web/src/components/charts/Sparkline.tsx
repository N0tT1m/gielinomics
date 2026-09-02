import { useMemo } from 'react'
import { extent, linePath, linearScale } from './scale'

interface Props {
  readonly values: readonly (number | null)[]
  readonly width?: number
  readonly height?: number
  readonly label: string
}

/**
 * A bare trend line for a table row.
 *
 * No axes, no ticks, no tooltip: a sparkline's job is shape at a glance, and chrome at this
 * size is noise. The exact figures live in the columns beside it.
 */
export function Sparkline({ values, width = 88, height = 24, label }: Props) {
  const path = useMemo(() => {
    const [lo, hi] = extent(values)
    const x = linearScale(0, Math.max(1, values.length - 1), 1, width - 1)
    const y = linearScale(lo, hi, height - 2, 2)
    return linePath(values.map((value, index) => ({ x: x(index), y: value === null ? null : y(value) })))
  }, [values, width, height])

  if (values.length < 2) return <span className="muted">—</span>

  return (
    <svg className="chart" width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label}>
      <path d={path} fill="none" stroke="var(--series-1)" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}
