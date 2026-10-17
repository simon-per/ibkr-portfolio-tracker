import { ChartColumn } from 'lucide-react'
import { useCurrencySymbol, useFormatCurrency } from '@/lib/CurrencyContext'
import { dividendAmount } from '@/lib/dividendFormat'
import { realizedOnlyYears } from '@/lib/dividendGrowth'
import { cn } from '@/lib/utils'
import { DeltaChip } from './DeltaChip'
import { DividendSubheading } from './DividendSubheading'
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
  const amount = (v: number) => dividendAmount(v, curSym)

  return (
    <section aria-label="Dividends per year" className="space-y-3">
      <DividendSubheading title="Per year" icon={ChartColumn} />
      <div className="space-y-1">
        {annual.map(row => {
          const actualPct = (row.net_eur / max) * 100
          const forecastPct = (row.forecast_net_eur / max) * 100
          const isSelected = selectedYear === row.year

          return (
            <button
              key={row.year}
              type="button"
              onClick={() => onSelectYear(row.year)}
              aria-pressed={isSelected}
              className={cn(
                'flex w-full items-center gap-3 rounded-lg px-2 py-1.5 text-left transition-colors',
                isSelected ? 'bg-accent ring-1 ring-border' : 'hover:bg-accent/50',
              )}
              title={
                `${row.year}: ${formatCurrency(row.net_eur)} received`
                + (row.forecast_net_eur > 0
                  ? ` plus ${formatCurrency(row.forecast_net_eur)} projected`
                  : '')
              }
            >
              <span className={cn(
                'w-11 shrink-0 text-sm tabular-nums',
                isSelected ? 'font-semibold text-foreground' : 'text-muted-foreground',
              )}>
                {row.year}
              </span>

              {/* Measured and projected in one bar, the projected part hatched —
                  the same convention the monthly chart uses.

                  Hidden below `sm`: the year, amount and chip columns are fixed
                  width, so at 390px the bar was squeezed to a 2px sliver that
                  carried no information while still taking the row's height. */}
              <span className="hidden h-5 min-w-0 flex-1 items-center overflow-hidden rounded-md bg-muted/50 sm:flex">
                <span
                  className="h-full rounded-l-md bg-primary/85"
                  style={{ width: `${actualPct}%` }}
                />
                <span
                  className="h-full rounded-r-md border-y border-r border-dashed border-primary/60 bg-primary/20"
                  style={{ width: `${forecastPct}%` }}
                />
              </span>

              <span className="flex-1 text-right text-sm font-medium tabular-nums sm:w-24 sm:flex-none">
                {amount(row.total_eur)}
                {/* Withheld at source — received plus expected, one figure. A quiet
                    reminder of the DA-1 threshold, not a tax figure. */}
                {withholding(row) > 0 && (
                  <span className="block text-[10px] font-normal leading-tight text-muted-foreground">
                    tax {amount(withholding(row))}
                  </span>
                )}
              </span>

              <span className="w-28 shrink-0 text-right sm:w-32">
                <DeltaChip
                  variant="pill"
                  pct={row.yoy_pct}
                  projected={row.yoy_includes_forecast}
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
    </section>
  )
}
