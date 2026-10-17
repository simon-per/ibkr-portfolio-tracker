import type { DividendPace } from '@/lib/dividendPace'
import { DeltaChip } from './DeltaChip'

interface DividendGrowthPaceProps {
  /** null whenever the selected range has no two windows to compare. */
  pace: DividendPace | null
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
 */
export function DividendGrowthPace({ pace }: DividendGrowthPaceProps) {
  if (!pace) return null

  return (
    <section
      aria-label="Dividend income growth"
      className="flex flex-wrap items-baseline gap-x-4 gap-y-1 rounded-xl bg-muted/50 px-4 py-3"
    >
      <span className="text-xs font-medium text-muted-foreground">Income growth</span>
      {/* flatBand 0 on both — the default band mutes lumpy series, and a rolling
          twelve-month total is the opposite of lumpy. */}
      <span className="inline-flex items-baseline gap-1.5">
        <DeltaChip
          pct={pace.cmgr_pct}
          label="per month"
          flatBand={0}
          projected={pace.includes_forecast}
          className="text-sm"
          title={`Compound monthly growth, ${pace.months} months`}
        />
      </span>
      {pace.cagr_pct != null && (
        <span className="inline-flex items-baseline gap-1.5">
          <DeltaChip
            pct={pace.cagr_pct}
            label="per year"
            flatBand={0}
            projected={pace.includes_forecast}
            className="text-sm"
            title="Compound annual growth"
          />
        </span>
      )}
    </section>
  )
}
