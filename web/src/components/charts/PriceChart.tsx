import { useMemo, useState } from 'react'
import type { PricePoint } from '../../api/client'
import { useElementWidth } from '../../hooks/useElementWidth'
import { exact, fullTime, gp, timeLabel } from './format'
import { extent, linePath, linearScale, plot, ticks } from './scale'

interface Props {
  readonly points: readonly PricePoint[]
  readonly height?: number
}

const SERIES = [
  { key: 'avgHigh', label: 'Instant-buy', color: 'var(--series-1)' },
  { key: 'avgLow', label: 'Instant-sell', color: 'var(--series-2)' },
] as const

/**
 * Two price series over time.
 *
 * One y-axis, never two: both series are gp, so they belong on the same scale — and the
 * distance between them *is* the spread, which a dual axis would render meaningless.
 */
export function PriceChart({ points, height = 260 }: Props) {
  const [wrapRef, width] = useElementWidth<HTMLDivElement>()
  const [hover, setHover] = useState<number | null>(null)

  const box = plot(width, height)

  const model = useMemo(() => {
    const [lo, hi] = extent(points.flatMap((point) => [point.avgHigh, point.avgLow]))
    return {
      x: linearScale(0, Math.max(1, points.length - 1), box.left, box.left + box.innerWidth),
      y: linearScale(lo, hi, box.top + box.innerHeight, box.top),
      lo,
      hi,
    }
  }, [points, box.left, box.innerWidth, box.top, box.innerHeight])

  const first = points[0]
  const last = points[points.length - 1]

  const spanMs = first && last ? new Date(last.bucketTs).getTime() - new Date(first.bucketTs).getTime() : 0
  const yTicks = ticks(model.lo, model.hi, 5)
  const xTickCount = Math.min(6, Math.max(2, points.length))
  const xTickIndices = Array.from({ length: xTickCount }, (_, index) =>
    Math.round((index * (points.length - 1)) / Math.max(1, xTickCount - 1)),
  )

  const activeIndex = hover
  const active = activeIndex === null ? undefined : points[activeIndex]

  // Direct labels collide whenever the two series end at a similar price, which for a
  // healthy item is most of the time — the spread is usually a couple of percent. Nudge
  // them apart rather than letting one overprint the other.
  const endLabels = (() => {
    const candidates = SERIES.flatMap((series) => {
      const latest = [...points].reverse().find((point) => point[series.key] !== null)
      if (!latest) return []
      return [
        {
          key: series.key,
          color: series.color,
          value: latest[series.key] as number,
          y: model.y(latest[series.key] as number),
        },
      ]
    }).sort((left, right) => left.y - right.y)

    const MIN_GAP = 13
    for (let i = 1; i < candidates.length; i++) {
      const previous = candidates[i - 1]!
      const current = candidates[i]!
      if (current.y - previous.y < MIN_GAP) current.y = previous.y + MIN_GAP
    }
    return candidates
  })()

  function onMove(event: React.PointerEvent<SVGSVGElement>) {
    const rect = event.currentTarget.getBoundingClientRect()

    // Scale the pointer into the SVG's own coordinate space: the element is responsive, so
    // client pixels and viewBox units are not the same thing.
    const svgX = ((event.clientX - rect.left) / rect.width) * width
    const ratio = (svgX - box.left) / box.innerWidth
    const index = Math.round(ratio * (points.length - 1))
    setHover(index >= 0 && index < points.length ? index : null)
  }

  return (
    <div className="chart-wrap" ref={wrapRef}>
      {points.length === 0 ? (
        <p className="empty">No retained history for this range yet.</p>
      ) : (
        <>
          <svg
            className="chart"
            viewBox={`0 0 ${width} ${height}`}
            height={height}
            role="img"
            aria-label="Instant-buy and instant-sell price over time"
            onPointerMove={onMove}
            onPointerLeave={() => setHover(null)}
          >
            {yTicks.map((tick) => (
              <g key={tick}>
                <line
                  className="grid-line"
                  x1={box.left}
                  x2={box.left + box.innerWidth}
                  y1={model.y(tick)}
                  y2={model.y(tick)}
                />
                <text
                  className="tick"
                  x={box.left - 8}
                  y={model.y(tick)}
                  textAnchor="end"
                  dominantBaseline="middle"
                >
                  {gp(tick)}
                </text>
              </g>
            ))}

            <line
              className="axis-line"
              x1={box.left}
              x2={box.left + box.innerWidth}
              y1={box.top + box.innerHeight}
              y2={box.top + box.innerHeight}
            />

            {xTickIndices.map((index) => {
              const point = points[index]
              if (!point) return null
              return (
                <text
                  key={index}
                  className="tick"
                  x={model.x(index)}
                  y={height - 6}
                  textAnchor={index === 0 ? 'start' : index === points.length - 1 ? 'end' : 'middle'}
                >
                  {timeLabel(point.bucketTs, spanMs)}
                </text>
              )
            })}

            {SERIES.map((series) => (
              <path
                key={series.key}
                className="series-line"
                stroke={series.color}
                d={linePath(
                  points.map((point, index) => ({
                    x: model.x(index),
                    y: point[series.key] === null ? null : model.y(point[series.key] as number),
                  })),
                )}
              />
            ))}

            {/* Direct labels at the line ends, so identity never rests on colour alone. */}
            {endLabels.map((label) => (
              <text
                key={label.key}
                className="end-label"
                fill={label.color}
                x={box.left + box.innerWidth + 6}
                y={label.y}
                dominantBaseline="middle"
              >
                {gp(label.value)}
              </text>
            ))}

            {active && activeIndex !== null && (
              <g>
                <line
                  className="crosshair"
                  x1={model.x(activeIndex)}
                  x2={model.x(activeIndex)}
                  y1={box.top}
                  y2={box.top + box.innerHeight}
                />
                {SERIES.map((series) =>
                  active[series.key] === null ? null : (
                    <circle
                      key={series.key}
                      className="marker"
                      cx={model.x(activeIndex)}
                      cy={model.y(active[series.key] as number)}
                      r={4.5}
                      fill={series.color}
                    />
                  ),
                )}
              </g>
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
                left: `clamp(0px, ${(model.x(activeIndex) / width) * 100}% - 80px, calc(100% - 170px))`,
                top: 4,
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
              <div className="tooltip-row">
                <span className="muted">Volume</span>
                <b>{exact(active.highVolume + active.lowVolume)}</b>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
