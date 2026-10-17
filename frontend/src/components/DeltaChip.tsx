import { cn } from '@/lib/utils'
import { deltaArrow, deltaColor, deltaTone, formatDelta, FLAT_BAND_PCT } from '@/lib/delta'

export interface DeltaChipProps {
  /** null renders a dash: the backend says there was no base to grow from. */
  pct: number | null | undefined
  /** What the percentage is measured against, e.g. "vs 2025". */
  label?: string
  /**
   * Marks the figure as forward-looking. A forecast compared against measured
   * income is a legitimate number but not the same kind of number, and the two
   * must not sit side by side looking identical.
   */
  projected?: boolean
  /**
   * Below this magnitude the chip is muted with no arrow. Defaults to the band that
   * suits lumpy series (dividends, monthly contributions); pass 0 where every move is
   * signal, such as a portfolio's period return.
   */
  flatBand?: number
  /**
   * `text` (default) is bare coloured text; `pill` sets the same figure on a soft wash of
   * its own colour, for a delta that sits beside a headline figure (`KpiCard`'s `delta`).
   */
  variant?: 'text' | 'pill'
  title?: string
  className?: string
}

const PILL: Record<'up' | 'down' | 'flat', string> = {
  up: 'bg-gain-soft',
  down: 'bg-loss-soft',
  flat: 'bg-muted',
}

/**
 * Signed percentage with a direction arrow, in the app's existing delta colours.
 *
 * Deliberately quiet: inside the flat band there is no arrow and no colour,
 * because dividend income is lumpy enough that a few percent is noise.
 */
export function DeltaChip({
  pct, label, projected, flatBand = FLAT_BAND_PCT, variant = 'text', title, className,
}: DeltaChipProps) {
  const arrow = deltaArrow(pct, flatBand)
  const hasValue = pct != null && Number.isFinite(pct)

  return (
    <span
      className={cn(
        'inline-flex flex-wrap items-center gap-x-1 text-xs tabular-nums [&>span]:whitespace-nowrap',
        hasValue ? deltaColor(pct, flatBand) : 'text-muted-foreground',
        variant === 'pill' && ['rounded-full px-2 py-0.5', PILL[deltaTone(pct, flatBand)]],
        className,
      )}
      title={title}
    >
      {arrow && <span aria-hidden="true">{arrow}</span>}
      <span className="font-medium">{formatDelta(pct)}</span>
      {label && <span className="font-normal text-muted-foreground">{label}</span>}
      {/* After the label, not before it: "+81.8% est. vs last 12M" reads as a
          stumble, "+81.8% vs last 12M · est." reads as an annotation. */}
      {projected && hasValue && (
        <span className="font-normal text-muted-foreground" title="Projected, not measured">
          · est.
        </span>
      )}
    </span>
  )
}
