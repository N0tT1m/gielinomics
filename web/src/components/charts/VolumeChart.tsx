import { useMemo, useState } from 'react'
import type { PricePoint } from '../../api/client'
import { useElementWidth } from '../../hooks/useElementWidth'
import { exact, fullTime, gp, timeLabel } from './format'
import { extent, linearScale, plot, ticks } from './scale'

interface Props {
  readonly points: readonly PricePoint[]
  readonly height?: number
}

const SERIES = [
  { key: 'highVolume', label: 'Bought', color: 'var(--series-1)' },
  { key: 'lowVolume', label: 'Sold', color: 'var(--series-2)' },
] as const

/** A 2px gap of surface between stacked segments, so the boundary reads without a stroke. */
const SEGMENT_GAP = 2

/**
 * Traded units per window, split by side of the book.
 *
 * Stacked rather than grouped: the total is the headline — is this item liquid enough to
 * flip at all — and the split is the follow-up. Measured from zero, because a bar length
 * only means anything against a zero baseline.
 */
export function VolumeChart({ points, height = 130 }: Props) {
  const [wrapRef, width] = useElementWidth<HTMLDivElement>()
  const [hover, setHover] = useState<number | null>(null)

  const box = plot(width, height, 56, 24, 8, 56)

  const model = useMemo(() => {
    const totals = points.map((point) => point.highVolume + point.lowVolume)
    const [, hi] = extent(totals, true)
    return {
      y: linearScale(0, hi, box.top + box.innerHeight, box.top),
      hi,
      band: box.innerWidth / Math.max(1, points.length),
    }
  }, [points, box.top, box.innerHeight, box.innerWidth])

  if (points.length === 0) return null

  const first = points[0]
  const last = points[points.length - 1]
  const spanMs = first && last ? new Date(last.bucketTs).getTime() - new Date(first.bucketTs).getTime() : 0

  // Bars sit inside their band with a gap, and never render narrower than a hairline.
  const barWidth = Math.max(1, Math.min(18, model.band * 0.7))
  const baseline = box.top + box.innerHeight
  const activeIndex = hover
  const active = activeIndex === null ? undefined : points[activeIndex]

  function onMove(event: React.PointerEvent<SVGSVGElement>) {
    const rect = event.currentTarget.getBoundingClientRect()
    const svgX = ((event.clientX - rect.left) / rect.width) * width
    const index = Math.floor((svgX - box.left) / model.band)
    setHover(index >= 0 && index < points.length ? index : null)
  }

  return (
    <div className="chart-wrap" ref={wrapRef}>
      <svg
        className="chart"
        viewBox={`0 0 ${width} ${height}`}
        height={height}
        role="img"
        aria-label="Traded volume per window, split into bought and sold"
        onPointerMove={onMove}
        onPointerLeave={() => setHover(null)}
      >
        {ticks(0, model.hi, 3).map((tick) => (
          <g key={tick}>
            <line
              className="grid-line"
              x1={box.left}
              x2={box.left + box.innerWidth}
              y1={model.y(tick)}
              y2={model.y(tick)}
            />
            <text className="tick" x={box.left - 8} y={model.y(tick)} textAnchor="end" dominantBaseline="middle">
              {gp(tick)}
            </text>
          </g>
        ))}

        {points.map((point, index) => {
          const x = box.left + index * model.band + (model.band - barWidth) / 2
          const boughtHeight = baseline - model.y(point.highVolume)
          const soldHeight = baseline - model.y(point.lowVolume)
          const dim = activeIndex !== null && activeIndex !== index

          return (
            <g key={point.bucketTs} opacity={dim ? 0.45 : 1}>
              {soldHeight > 0 && (
                <rect
                  x={x}
                  y={baseline - soldHeight}
                  width={barWidth}
                  height={soldHeight}
                  fill="var(--series-2)"
                  rx={2}
                />
              )}
              {boughtHeight > 0 && (
                <rect
                  x={x}
                  y={baseline - soldHeight - boughtHeight - (soldHeight > 0 ? SEGMENT_GAP : 0)}
                  width={barWidth}
                  height={boughtHeight}
                  fill="var(--series-1)"
                  rx={2}
                />
              )}
            </g>
          )
        })}

        <line
          className="axis-line"
          x1={box.left}
          x2={box.left + box.innerWidth}
          y1={baseline}
          y2={baseline}
        />

        {first && last && (
          <>
            <text className="tick" x={box.left} y={height - 6} textAnchor="start">
              {timeLabel(first.bucketTs, spanMs)}
            </text>
            <text className="tick" x={box.left + box.innerWidth} y={height - 6} textAnchor="end">
              {timeLabel(last.bucketTs, spanMs)}
            </text>
          </>
        )}
      </svg>

      <div className="legend" style={{ marginTop: 6 }}>
        {SERIES.map((series) => (
          <span className="legend-item" key={series.key}>
            <span className="swatch" style={{ background: series.color }} />
            {series.label}
          </span>
        ))}
      </div>

      {active && activeIndex !== null && (
        <div
          className="tooltip"
          style={{
            left: `clamp(0px, ${((box.left + activeIndex * model.band) / width) * 100}% - 80px, calc(100% - 170px))`,
            top: 0,
          }}
        >
          <div className="tooltip-title">{fullTime(active.bucketTs)}</div>
          {SERIES.map((series) => (
            <div className="tooltip-row" key={series.key}>
              <span className="legend-item">
                <span className="swatch" style={{ background: series.color }} />
                {series.label}
              </span>
              <b>{exact(active[series.key])}</b>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
