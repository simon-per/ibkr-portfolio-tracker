import type { DividendTtmPace } from '@/lib/api'
import { useFormatCurrency } from '@/lib/CurrencyContext'
import { dividendTtmWindowLabel, NOT_PER_SHARE_CAVEAT } from '@/lib/dividendChart'
import { DeltaChip } from './DeltaChip'

const ERA_CAVEAT =
  'The two windows this rate is measured between span the switch from Yahoo Finance '
  + 'estimates to IBKR actuals, so part of the change is a change of source.'

const FORECAST_NOTE =
  'A projected window assumes every holding keeps paying at its current per-share '
  + 'rate on its own cadence, so a forward pace reflects the payout schedule as much '
  + 'as growth. Turn Forecast off for the rate between windows that have fully elapsed.'

interface DividendGrowthPaceProps {
  /** null whenever there are not two covered windows to compare. */
  pace: DividendTtmPace | null | undefined
}

/**
 * How fast the rolling twelve-month total is moving, per month and per year.
 *
 * A strip rather than a tile row: the two figures are one quantity in two units,
 * and giving each a tile of its own would present them as two findings. It sits
 * under the four KPI tiles because it is derived from the chart below it, and it
 * stays on screen in both chart modes — the rate is a fact about the income, not
 * an annotation on one view of it.
 *
 * Renders nothing at all when the server sends no pace. A dash beside "growth
 * pace" would still occupy the reader with a figure that does not exist; the
 * chart below already explains that a rolling total needs twelve months behind
 * it.
 */
export function DividendGrowthPace({ pace }: DividendGrowthPaceProps) {
  const formatCurrency = useFormatCurrency()
  if (!pace) return null

  const caveat = pace.crosses_era ? ERA_CAVEAT : undefined

  return (
    <section
      aria-label="Dividend growth pace"
      className="space-y-1 rounded-lg border border-border bg-card/50 px-4 py-3"
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
        <span className="text-xs font-medium text-muted-foreground">Growth pace</span>
        {/* flatBand 0 on both: the default band exists to mute lumpy series, and a
            rolling window geometrically averaged is the opposite of lumpy. Muting
            2%/month as noise would hide a rate that compounds to +27% a year. */}
        <DeltaChip
          pct={pace.monthly_pct}
          label="per month"
          flatBand={0}
          projected={pace.includes_forecast}
          caveat={caveat}
          className="text-sm"
        />
        <DeltaChip
          pct={pace.annualized_pct}
          label="a year"
          flatBand={0}
          projected={pace.includes_forecast}
          caveat={caveat}
          className="text-sm"
          title={
            `The monthly rate compounded over twelve months, not multiplied by twelve.`
          }
        />
      </div>

      <p className="text-xs text-muted-foreground">
        {dividendTtmWindowLabel(pace.to_month)} against {dividendTtmWindowLabel(pace.from_month)}
        {' · '}
        {formatCurrency(pace.from_eur)} → {formatCurrency(pace.to_eur)}
        {/* The span travels with the figure rather than living in a tooltip: a rate
            labelled "per month" that rests on three of them is a different claim. */}
        {pace.short_history
          ? ` · over ${pace.months} month${pace.months === 1 ? '' : 's'} of the rolling series, not 6`
          : ''}
      </p>

      {pace.includes_forecast && (
        <p className="text-xs text-muted-foreground">{FORECAST_NOTE}</p>
      )}
      <p className="text-xs text-muted-foreground">{NOT_PER_SHARE_CAVEAT}</p>
    </section>
  )
}
