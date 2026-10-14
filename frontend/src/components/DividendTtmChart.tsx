import type { DividendTtmPoint } from '@/lib/api'
import { useFormatCurrency } from '@/lib/CurrencyContext'
import { dividendTtmWindowLabel } from '@/lib/dividendChart'
import { cn } from '@/lib/utils'
import { DeltaChip } from './DeltaChip'
import { DIVIDEND_CHART_BOX, DividendStackChart } from './DividendStackChart'

interface DividendTtmChartProps {
  /** Rows to draw — already filtered and ordered to match `points`. */
  data: Record<string, number | string>[]
  points: DividendTtmPoint[]
  stackSymbols: string[]
  colorOf: (symbol: string) => string
  showForecast: boolean
  multiYear: boolean
  /** Highlighted from the legend; passed straight through to the shared chart. */
  activeSymbol?: string | null
}

/**
 * Rolling twelve-month dividend income, stacked by the holdings that paid it.
 *
 * Stacked rather than a single line because the question a trailing total raises
 * is immediately "which holdings are carrying this" — and a line cannot answer
 * it. Same stack, same colours and the same translucent-dashed forecast segments
 * as the monthly chart, so nothing new has to be learned to read it.
 *
 * Every point the server sends has twelve months of history behind it; the ones
 * it cannot cover are absent rather than drawn as a gap. With projections hidden
 * the open windows go too, since those would report only the part of themselves
 * that has already happened.
 */
export function DividendTtmChart({
  data, points, stackSymbols, colorOf, showForecast, multiYear, activeSymbol = null,
}: DividendTtmChartProps) {
  const formatCurrency = useFormatCurrency()
  const latest = points.at(-1)
  const byMonth = new Map(points.map((p) => [p.month, p]))

  return (
    <section aria-label="Rolling twelve month dividends" className="space-y-3">
      <div className="space-y-1">
        <div className="text-sm font-medium">Rolling 12 months by holding</div>
        {latest && (
          <div
            className="flex flex-wrap items-baseline gap-x-3 gap-y-1"
            aria-label="Latest rolling 12 months"
          >
            <span className="text-xl font-semibold tabular-nums">
              {formatCurrency(latest.total_eur)}
            </span>
            <span className="text-xs text-muted-foreground">
              {dividendTtmWindowLabel(latest.month)}
            </span>
            {/* The chip's `est.` marks the CHANGE as forward-looking; the amount
                needs its own marker, on the surface rather than in a title. */}
            {latest.partial && (
              <span className="text-xs text-muted-foreground">
                {latest.forecast_net_eur > 0
                  ? `incl. ${formatCurrency(latest.forecast_net_eur)} projected`
                  : 'window not yet complete'}
              </span>
            )}
            <DeltaChip
              pct={latest.mom_pct}
              label="vs previous window"
              flatBand={0}
              projected={latest.mom_includes_forecast}
            />
          </div>
        )}
      </div>

      {!latest ? (
        <div
          className={cn(
            DIVIDEND_CHART_BOX,
            'flex items-center justify-center text-center text-sm text-muted-foreground',
          )}
        >
          No twelve-month window is covered for this period yet. A rolling total needs
          twelve months of recorded history behind it.
        </div>
      ) : (
        <DividendStackChart
          data={data}
          stackSymbols={stackSymbols}
          colorOf={colorOf}
          showForecast={showForecast}
          multiYear={multiYear}
          activeSymbol={activeSymbol}
          tooltipTitle={dividendTtmWindowLabel}
          tooltipFooter={(month) => {
            const p = byMonth.get(month)
            if (!p) return null
            return (
              <DeltaChip
                pct={p.mom_pct}
                label="vs previous window"
                flatBand={0}
                projected={p.mom_includes_forecast}
              />
            )
          }}
        />
      )}

    </section>
  )
}
