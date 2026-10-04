import { useCurrencySymbol, useFormatCurrency } from '@/lib/CurrencyContext'
import { realizedOnlyYears } from '@/lib/dividendGrowth'
import { DeltaChip } from './DeltaChip'
import type { DividendAnnualRow } from '@/lib/api'

interface DividendYearComparisonProps {
  annual: DividendAnnualRow[]
  /** Highlights the year the chart is currently filtered to. */
  selectedYear: number | 'all'
  onSelectYear: (year: number) => void
  /** When false, the panel shows realized income only (see realizedOnlyYears). */
  showForecast: boolean
}

export function DividendYearComparison({
  annual: allRows, selectedYear, onSelectYear, showForecast,
}: DividendYearComparisonProps) {
  const curSym = useCurrencySymbol()
  const formatCurrency = useFormatCurrency()

  const annual = showForecast ? allRows : realizedOnlyYears(allRows)
  if (annual.length === 0) return null

  const max = Math.max(...annual.map(a => a.total_eur), 0.01)
  const withholding = (row: DividendAnnualRow) =>
    (row.withholding_eur ?? 0) + (row.forecast_withholding_eur ?? 0)
  // Cents matter at this scale — the earliest year here is under 2 units, which
  // whole-number rounding would flatten to "2" beside a 116.
  const amount = (v: number) => v.toLocaleString('en-US', {
    minimumFractionDigits: Math.abs(v) < 1000 ? 2 : 0,
    maximumFractionDigits: Math.abs(v) < 1000 ? 2 : 0,
  })

  return (
    <div className="space-y-2">
      <div className="text-sm font-medium">Per year</div>
      <div className="space-y-1.5">
        {annual.map(row => {
          const actualPct = (row.net_eur / max) * 100
          const forecastPct = (row.forecast_net_eur / max) * 100
          const isSelected = selectedYear === row.year

          const caveat = row.yoy_vs_partial
            ? 'The year it is compared against does not cover twelve full months, '
              + 'so this overstates the growth.'
            : undefined

          return (
            <button
              key={row.year}
              type="button"
              onClick={() => onSelectYear(row.year)}
              aria-pressed={isSelected}
              className={
                'flex w-full items-center gap-3 rounded px-2 py-1.5 text-left transition-colors '
                + (isSelected ? 'bg-accent' : 'hover:bg-accent/50')
              }
              title={
                `${row.year}: ${formatCurrency(row.net_eur)} received`
                + (row.forecast_net_eur > 0
                  ? ` plus ${formatCurrency(row.forecast_net_eur)} projected`
                  : '')
                + (row.partial
                  ? ' — this year is not complete, so the figure covers part of it only'
                  : '')
                + '. Click to filter the chart to this year.'
              }
            >
              <span className="w-11 shrink-0 text-sm tabular-nums text-muted-foreground">
                {row.year}
              </span>

              {/* Measured and projected in one bar, the projected part hatched —
                  the same convention the monthly chart uses.

                  Hidden below `sm`: the year, amount and chip columns are fixed
                  width, so at 390px the bar was squeezed to a 2px sliver that
                  carried no information while still taking the row's height. */}
              <span className="hidden h-5 min-w-0 flex-1 items-center overflow-hidden rounded-sm bg-muted/40 sm:flex">
                <span
                  className="h-full bg-primary/80"
                  style={{ width: `${actualPct}%` }}
                />
                <span
                  className="h-full border-y border-r border-dashed border-primary/60 bg-primary/25"
                  style={{ width: `${forecastPct}%` }}
                />
              </span>

              <span className="flex-1 text-right text-sm font-medium tabular-nums sm:w-24 sm:flex-none">
                {curSym}{amount(row.total_eur)}
                {row.partial && (
                  <span className="text-muted-foreground" title="Incomplete year">*</span>
                )}
                {/* Withheld at source — received plus expected, one figure. A quiet
                    reminder of the DA-1 threshold, not a tax figure. */}
                {withholding(row) > 0 && (
                  <span className="block text-[10px] font-normal leading-tight text-muted-foreground">
                    WHT {curSym}{amount(withholding(row))}
                  </span>
                )}
              </span>

              <span className="w-28 shrink-0 text-right sm:w-32">
                <DeltaChip
                  pct={row.yoy_pct}
                  projected={row.yoy_includes_forecast}
                  caveat={caveat}
                  title={
                    row.yoy_pct == null
                      ? 'No comparable preceding year'
                      : `Against ${row.year - 1}`
                  }
                />
              </span>
            </button>
          )
        })}
      </div>
      {annual.some(a => a.partial) && (
        <div className="text-xs text-muted-foreground">
          * incomplete year — still running, or the dividend history begins inside it
        </div>
      )}
      {annual.some(a => a.yoy_vs_partial && a.yoy_pct != null) && (
        <div className="text-xs text-muted-foreground">
          <span className="text-amber-600 dark:text-amber-500">†</span> compared against
          an incomplete year, so the percentage overstates the growth
        </div>
      )}
    </div>
  )
}
