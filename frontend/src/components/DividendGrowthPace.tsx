import { useFormatCurrency } from '@/lib/CurrencyContext'
import { dividendMonthLabel } from '@/lib/dividendChart'
import type { DividendPace } from '@/lib/dividendPace'
import { DeltaChip } from './DeltaChip'

const RAMP_CAVEAT =
  'Measured from the earliest twelve-month window on record. The portfolio was '
  + 'still being funded inside it, so the rate reflects money going in as much as '
  + 'dividends going up. Narrow the range for a like-for-like read.'

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
  const formatCurrency = useFormatCurrency()
  if (!pace) return null

  const caveat = pace.coverage_limited ? RAMP_CAVEAT : undefined

  return (
    <section
      aria-label="Dividend income growth"
      className="flex flex-wrap items-baseline gap-x-4 gap-y-1 rounded-lg border border-border bg-card/50 px-4 py-3"
    >
      <span className="text-xs font-medium text-muted-foreground">Income growth</span>
      {/* The acronym leads and the unit trails it: "CMGR +15.3% /mo" is how the
          figure is said, where DeltaChip's own value-then-label order would give
          "+15.3% CMGR". flatBand 0 on both — the default band mutes lumpy series,
          and a rolling twelve-month total is the opposite of lumpy. */}
      <span className="inline-flex items-baseline gap-1.5">
        <span className="text-xs font-medium text-muted-foreground">CMGR</span>
        <DeltaChip
          pct={pace.cmgr_pct}
          label="/mo"
          flatBand={0}
          projected={pace.includes_forecast}
          caveat={caveat}
          className="text-sm"
          title={`Compound monthly growth rate over ${pace.months} months.`}
        />
      </span>
      {pace.cagr_pct != null && (
        <span className="inline-flex items-baseline gap-1.5">
          <span className="text-xs font-medium text-muted-foreground">CAGR</span>
          <DeltaChip
            pct={pace.cagr_pct}
            label="/yr"
            flatBand={0}
            projected={pace.includes_forecast}
            caveat={caveat}
            className="text-sm"
            title="The monthly rate compounded over twelve months, not multiplied by twelve."
          />
        </span>
      )}
      {/* The two windows it is measured between, so the figure can be checked
          against the first and last bar of the chart below — and, when the base is
          the earliest window there is, four words saying so. The dagger carries the
          full reason, but a marker whose meaning lives only in a hover is one the
          reader on a phone never gets. */}
      <span className="text-xs text-muted-foreground">
        {dividendMonthLabel(pace.from_month, true)} → {dividendMonthLabel(pace.to_month, true)}
        {' · '}
        {formatCurrency(pace.from_eur)} → {formatCurrency(pace.to_eur)}
        {pace.coverage_limited && ' · from the first window on record'}
      </span>
    </section>
  )
}
