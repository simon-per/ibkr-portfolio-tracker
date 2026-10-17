import { TrendingDown, TrendingUp } from 'lucide-react'
import type { DividendPace } from '@/lib/dividendPace'
import { deltaTone } from '@/lib/delta'
import type { Tone } from '@/lib/tone'
import { IconChip } from '@/components/ui/IconChip'
import { DeltaChip } from './DeltaChip'

interface DividendGrowthPaceProps {
  /** null whenever the selected range has no two windows to compare. */
  pace: DividendPace | null
}

const WELL: Record<'up' | 'down' | 'flat', Tone> = {
  up: 'positive', down: 'negative', flat: 'neutral',
}

/**
 * How fast dividend income grew across the windows currently on screen.
 *
 * "Income growth", not "growth pace": naming what is measured is what keeps a
 * reader from taking it for per-share dividend increases, and it does in one
 * word what a sentence of caveat used to do.
 *
 * A strip rather than tiles. The four KPI tiles above are all range-independent
 * (365 days through today, this year to date), and six tiles that look alike
 * where two follow the range selector and four ignore it is the confusing
 * version of this.
 *
 * The well takes the monthly rate's direction, read through the same `deltaTone` as the
 * pills beside it, so the icon cannot say "up" while the figure says "flat".
 */
export function DividendGrowthPace({ pace }: DividendGrowthPaceProps) {
  if (!pace) return null
  const direction = deltaTone(pace.cmgr_pct, 0)

  return (
    <section
      aria-label="Dividend income growth"
      className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border border-border/70 bg-raised px-4 py-3"
    >
      <span className="inline-flex items-center gap-2">
        <IconChip icon={direction === 'down' ? TrendingDown : TrendingUp} tone={WELL[direction]} />
        <span className="text-xs font-medium text-muted-foreground">Income growth</span>
      </span>
      {/* flatBand 0 on both — the default band mutes lumpy series, and a rolling
          twelve-month total is the opposite of lumpy. */}
      <DeltaChip
        variant="pill"
        pct={pace.cmgr_pct}
        label="per month"
        flatBand={0}
        projected={pace.includes_forecast}
        className="text-sm"
        title={`Compound monthly growth, ${pace.months} months`}
      />
      {pace.cagr_pct != null && (
        <DeltaChip
          variant="pill"
          pct={pace.cagr_pct}
          label="per year"
          flatBand={0}
          projected={pace.includes_forecast}
          className="text-sm"
          title="Compound annual growth"
        />
      )}
    </section>
  )
}
