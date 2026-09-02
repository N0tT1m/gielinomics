/** Inner drawing area after axis gutters. */
export interface Plot {
  readonly width: number
  readonly height: number
  readonly left: number
  readonly top: number
  readonly right: number
  readonly bottom: number
  readonly innerWidth: number
  readonly innerHeight: number
}

export function plot(width: number, height: number, left = 56, bottom = 24, top = 12, right = 56): Plot {
  return {
    width,
    height,
    left,
    top,
    right,
    bottom,
    innerWidth: Math.max(1, width - left - right),
    innerHeight: Math.max(1, height - top - bottom),
  }
}

/** Maps a domain onto a pixel range. */
export interface Scale {
  (value: number): number
  readonly min: number
  readonly max: number
}

export function linearScale(min: number, max: number, from: number, to: number): Scale {
  // A flat series has zero extent; without this it would divide by zero and render NaN paths.
  const span = max - min || 1
  const scale = (value: number) => from + ((value - min) / span) * (to - from)
  return Object.assign(scale, { min, max })
}

/**
 * Extent of the values, padded and with a sensible floor.
 *
 * Price series never start at zero — an item that trades between 800k and 810k would be a
 * flat line against a zero baseline, hiding the only movement there is. Volume charts pass
 * `includeZero` because a bar length is only meaningful measured from zero.
 */
export function extent(values: readonly (number | null | undefined)[], includeZero = false): [number, number] {
  let min = Number.POSITIVE_INFINITY
  let max = Number.NEGATIVE_INFINITY

  for (const value of values) {
    if (value === null || value === undefined || !Number.isFinite(value)) continue
    if (value < min) min = value
    if (value > max) max = value
  }

  if (!Number.isFinite(min) || !Number.isFinite(max)) return [0, 1]
  if (includeZero) min = Math.min(0, min)

  if (min === max) {
    const pad = Math.abs(min) * 0.05 || 1
    return [min - pad, max + pad]
  }

  const pad = (max - min) * 0.08
  return [min - (includeZero ? 0 : pad), max + pad]
}

/** Round tick values across a domain, at most `count` of them. */
export function ticks(min: number, max: number, count = 5): number[] {
  const span = max - min
  if (span <= 0) return [min]

  const rough = span / Math.max(1, count)
  const magnitude = 10 ** Math.floor(Math.log10(rough))
  const normalised = rough / magnitude
  const step = (normalised >= 5 ? 10 : normalised >= 2 ? 5 : normalised >= 1 ? 2 : 1) * magnitude

  const out: number[] = []
  for (let value = Math.ceil(min / step) * step; value <= max; value += step) {
    out.push(Number(value.toFixed(10)))
  }
  return out
}

/**
 * Builds a path, breaking it wherever the series has no value.
 *
 * A gap is a window in which nothing traded. Interpolating across it would draw a
 * confident straight line through data this platform does not have, which is precisely
 * the claim the ingest audit trail exists to avoid making.
 */
export function linePath(
  points: readonly { x: number; y: number | null }[],
): string {
  let path = ''
  let pendingMove = true

  for (const point of points) {
    if (point.y === null) {
      pendingMove = true
      continue
    }
    path += `${pendingMove ? 'M' : 'L'}${point.x.toFixed(2)},${point.y.toFixed(2)}`
    pendingMove = false
  }

  return path
}
