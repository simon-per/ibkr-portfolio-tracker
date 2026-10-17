import { useId } from 'react'
import { TONE_MARK, type Tone } from '@/lib/tone'
import { cn } from '@/lib/utils'
import { SPARK_H, SPARK_W, sparklineSegments } from '@/lib/sparkline'

interface SparklineProps {
  /** In time order. A non-finite entry is a gap in the line, never a zero. */
  values: readonly (number | null | undefined)[]
  /**
   * The stroke. Neutral is the brand blue; omit it and the line takes the sign of its own
   * change, last measured point against first.
   */
  tone?: Tone
  /** Soft fill under the line. On by default; off for a dense row. */
  area?: boolean
  /** An accessible name ("Value, last 30 days"); without one the line is decorative. */
  label?: string
  className?: string
}

/**
 * A tiny inline trend for a KPI: plain SVG, no Recharts, no axes, no tooltip. It shows a
 * shape, not values — the figure beside it is the value.
 */
export function Sparkline({ values, tone, area = true, label, className }: SparklineProps) {
  // An SVG fragment reference matches the id literally, and useId's markers are not
  // characters every engine accepts there.
  const gradientId = `spark${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`
  const drawn = sparklineSegments(values)
  if (!drawn) return null

  const resolved: Tone = tone
    ?? (drawn.last > drawn.first ? 'positive' : drawn.last < drawn.first ? 'negative' : 'neutral')
  const path = (pts: [number, number][]) =>
    pts.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(2)},${y.toFixed(2)}`).join('')

  return (
    <svg
      viewBox={`0 0 ${SPARK_W} ${SPARK_H}`}
      preserveAspectRatio="none"
      className={cn('block h-8 w-full overflow-visible', TONE_MARK[resolved], className)}
      {...(label ? { role: 'img', 'aria-label': label } : { 'aria-hidden': true })}
    >
      {area && (
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="currentColor" stopOpacity="0.22" />
            <stop offset="1" stopColor="currentColor" stopOpacity="0" />
          </linearGradient>
        </defs>
      )}
      {drawn.segments.map((pts, i) => (
        <g key={i}>
          {area && pts.length > 1 && (
            <path
              d={`${path(pts)}L${pts[pts.length - 1][0].toFixed(2)},${SPARK_H}L${pts[0][0].toFixed(2)},${SPARK_H}Z`}
              fill={`url(#${gradientId})`}
              stroke="none"
            />
          )}
          <path
            d={path(pts)}
            fill="none"
            stroke="currentColor"
            strokeWidth={1.5}
            strokeLinecap="round"
            strokeLinejoin="round"
            vectorEffect="non-scaling-stroke"
          />
        </g>
      ))}
    </svg>
  )
}
