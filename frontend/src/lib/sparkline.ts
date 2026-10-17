/**
 * The geometry behind `ui/Sparkline`: values to drawable segments, apart from the component so
 * it can be tested without a DOM and the component module exports components only.
 */

// The drawing box. `preserveAspectRatio="none"` stretches it to whatever the class sizes,
// and `vector-effect` keeps the stroke 1.5px however far it is stretched.
export const SPARK_W = 100
export const SPARK_H = 32
const PAD = 2

/** The points to draw, split at every gap, or null when fewer than two are measurable. */
export function sparklineSegments(
  values: readonly (number | null | undefined)[],
): { segments: [number, number][][]; first: number; last: number } | null {
  const finite = values.filter((v): v is number => v != null && Number.isFinite(v))
  if (finite.length < 2) return null
  const min = Math.min(...finite)
  const max = Math.max(...finite)
  const span = max - min
  const step = values.length > 1 ? SPARK_W / (values.length - 1) : 0
  const segments: [number, number][][] = []
  let current: [number, number][] = []
  values.forEach((v, i) => {
    if (v == null || !Number.isFinite(v)) {
      if (current.length) segments.push(current)
      current = []
      return
    }
    // A flat series draws along the middle rather than dividing by zero.
    const y = span === 0 ? SPARK_H / 2 : PAD + (1 - (v - min) / span) * (SPARK_H - 2 * PAD)
    current.push([i * step, y])
  })
  if (current.length) segments.push(current)
  return { segments, first: finite[0], last: finite[finite.length - 1] }
}
