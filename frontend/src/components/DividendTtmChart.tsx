import {
  CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import type { DividendMonthBar } from '@/lib/api'
import { useCurrencySymbol, useFormatCurrency } from '@/lib/CurrencyContext'
import { dividendMonthLabel, dividendTtmWindowLabel } from '@/lib/dividendChart'
import { useIsCompact } from '@/lib/useMediaQuery'
import { DeltaChip } from './DeltaChip'

interface Props {
  months: DividendMonthBar[]
  multiYear: boolean
}

export function DividendTtmChart({ months, multiYear }: Props) {
  const formatCurrency = useFormatCurrency()
  const curSym = useCurrencySymbol()
  const isCompact = useIsCompact()
  const available = months.filter(m => m.ttm_net_eur != null)
  const latest = available.at(-1)
  const includesEstimates = available.some(m => m.ttm_source === 'mixed' || m.ttm_source === 'yfinance_estimate')
  const crossesEra = available.some(m => m.ttm_mom_crosses_era)

  return (
    <section aria-label="Trailing twelve month dividends" className="space-y-3">
      <div className="space-y-1">
        <div className="text-sm font-medium">12 completed calendar months</div>
        <p className="text-xs text-muted-foreground">
          Each point totals the ending month and the preceding 11 months. The current month
          and forecasts are excluded. The Last 12 months card above runs through today instead.
        </p>
        {latest && (
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1" aria-label="Latest completed TTM">
            <span className="text-xl font-semibold tabular-nums">{formatCurrency(latest.ttm_net_eur!)}</span>
            <span className="text-xs text-muted-foreground">{dividendTtmWindowLabel(latest.month)}</span>
            <DeltaChip pct={latest.ttm_mom_pct} label="vs previous TTM month" flatBand={0} />
          </div>
        )}
      </div>

      {!latest ? (
        <div className="flex h-[240px] items-center justify-center text-center text-sm text-muted-foreground sm:h-[320px]">
          No completed 12-month window available for this period. TTM needs 12 months of recorded history.
        </div>
      ) : (
        <div className="h-[240px] w-full sm:h-[320px]">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={months} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" className="stroke-muted" vertical={false} />
              <XAxis
                dataKey="month"
                tickFormatter={(m: string) => dividendMonthLabel(m, multiYear)}
                tick={{ fill: 'hsl(var(--muted-foreground))', fontSize: 12 }}
                tickLine={false}
                axisLine={{ stroke: 'hsl(var(--border))' }}
                interval={multiYear ? 'preserveStartEnd' : isCompact ? 1 : 0}
                minTickGap={isCompact ? 24 : 16}
              />
              <YAxis
                tickFormatter={(v: number) => Math.abs(v) >= 1000 ? `${curSym}${(v / 1000).toFixed(1)}k` : `${curSym}${v}`}
                tick={{ fill: 'hsl(var(--muted-foreground))', fontSize: 12 }}
                tickLine={false}
                axisLine={false}
                width={isCompact ? 40 : 60}
              />
              <Tooltip content={({ active, payload }) => {
                const point = payload?.[0]?.payload as DividendMonthBar | undefined
                if (!active || point?.ttm_net_eur == null) return null
                return (
                  <div className="rounded-lg border border-border bg-card px-3 py-2 text-sm shadow-md">
                    <div className="font-medium">{dividendTtmWindowLabel(point.month)}</div>
                    <div className="my-1 tabular-nums">TTM {formatCurrency(point.ttm_net_eur)}</div>
                    <DeltaChip pct={point.ttm_mom_pct} label="vs previous TTM month" flatBand={0} />
                    {point.ttm_source && point.ttm_source !== 'ibkr' && (
                      <div className="mt-1 text-xs text-muted-foreground">Includes historical gross estimates</div>
                    )}
                  </div>
                )
              }} />
              <Line
                type="linear"
                dataKey="ttm_net_eur"
                name="TTM dividends"
                stroke="hsl(var(--primary))"
                strokeWidth={2}
                dot={available.length === 1 ? { r: 4 } : false}
                activeDot={{ r: 4 }}
                connectNulls={false}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
      <div className="space-y-1 text-xs text-muted-foreground">
        {includesEstimates && (
          <p>Earlier history includes estimated gross dividends; withholding is not deducted from those estimates.</p>
        )}
        {crossesEra && (
          <p>Some TTM comparisons span the switch from estimates to IBKR actuals, so the change also reflects a change of source.</p>
        )}
        <p>Portfolio dividend income also changes with holdings and exchange rates; this is not a measure of dividend increases per share.</p>
      </div>
    </section>
  )
}
