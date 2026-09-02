import { useEffect, useRef, useState } from 'react'

/**
 * Tracks an element's width so an SVG chart can size itself without a layout library.
 *
 * Charts are drawn in viewBox units and scaled by CSS, but the *number* of ticks and the
 * pointer-to-index maths both need the real pixel width — a chart that assumes 720px puts
 * its crosshair in the wrong place on every other screen.
 */
export function useElementWidth<T extends HTMLElement>(initial = 720) {
  const ref = useRef<T>(null)
  const [width, setWidth] = useState(initial)

  useEffect(() => {
    const element = ref.current
    if (!element) return

    const observer = new ResizeObserver((entries) => {
      const entry = entries[0]
      if (entry) setWidth(Math.max(320, Math.floor(entry.contentRect.width)))
    })

    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  return [ref, width] as const
}
