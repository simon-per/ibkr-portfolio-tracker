import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceArea,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api, type DividendPerShareGrowth } from '@/lib/api'
import {
  CHART_TOOLTIP_ITEM_STYLE,
  CHART_TOOLTIP_LABEL_STYLE,
  CHART_TOOLTIP_STYLE,
} from '@/lib/chartTooltip'
import { useCurrencySymbol, useFormatCurrency } from '@/lib/CurrencyContext'
import { forecastTrend, partLabel } from '@/lib/dividendForecastTrend'
import { axisFloor, niceTicks } from '@/lib/niceTicks'
import { useIsCompact } from '@/lib/useMediaQuery'
import { formatDate, parseLocalDate, tooltipValue } from '@/lib/utils'
import { DeltaChip } from './DeltaChip'

interface DividendForecastTrendProps {
  /** The payers' own growth, from the breakdown — the part that needs no purchase. */
  perShare?: DividendPerShareGrowth | null
}

function shortDay(iso: string): string {
  const date = parseLocalDate(iso)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

/**
 * The next-12-month forecast since the day growth is measured from.
 *
 * One question, "is the income this book will pay growing", and one line to answer it.
 * What the growth was made of sits underneath as a sentence rather than a second chart:
 * on a book still being funded it is nearly all shares bought, which is worth knowing
 * once and not worth a stacked bar every day. Renders nothing until there are two days
 * to connect.
 */
export function DividendForecastTrend({ perShare }: DividendForecastTrendProps) {
  const formatCurrency = useFormatCurrency()
  const curSym = useCurrencySymbol()
  const isCompact = useIsCompact()
  const { data } = useQuery({
    queryKey: ['dividends', 'forecast-history'],
    queryFn: () => api.getDividendForecastHistory(),
  })
  const trend = useMemo(() => forecastTrend(data), [data])

  const axis = useMemo(() => {
    if (!trend) return null
    const values = trend.points.map((p) => p.value)
    const min = Math.min(...values)
    const max = Math.max(...values)
    const pad = (max - min || Math.abs(max) || 1) * 0.15
    return niceTicks(min - pad, max + pad, 3, axisFloor(min, min - pad, min - pad))
  }, [trend])

  if (!trend || !axis || trend.points.length < 2) return null

  const signed = (value: number) =>
    `${value < 0 ? '−' : '+'}${formatCurrency(Math.abs(value))}`

  return (
    <section
      aria-label="Dividend forecast over time"
      className="space-y-2 rounded-lg border border-border bg-card/50 px-4 py-3"
    >
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="text-xs font-medium text-muted-foreground">Next 12 months</span>
        <span className="text-sm font-semibold tabular-nums">
          {formatCurrency(trend.latest)}
        </span>
        <DeltaChip
          pct={trend.sincePct}
          label={`since ${shortDay(trend.start)}`}
          flatBand={0}
          projected
          className="text-sm"
          title="Growth of the projected next-12-month income since the day it is measured from."
        />
      </div>

      <div className="h-[120px] w-full">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={trend.points} margin={{ top: 4, right: isCompact ? 8 : 16, left: 0, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" className="stroke-muted" vertical={false} />
            <XAxis
              dataKey="date"
              className="text-xs"
              tick={{ fill: 'hsl(var(--muted-foreground))' }}
              tickFormatter={shortDay}
              interval="preserveStartEnd"
              minTickGap={isCompact ? 44 : 60}
            />
            <YAxis
              className="text-xs"
              tick={{ fill: 'hsl(var(--muted-foreground))' }}
              tickFormatter={(value: number) => `${curSym}${Math.round(value)}`}
              domain={axis.domain}
              ticks={axis.ticks}
              width={isCompact ? 44 : 56}
            />
            {trend.replayed && (
              <ReferenceArea
                x1={trend.replayed.from}
                x2={trend.replayed.to}
                fill="hsl(var(--muted-foreground))"
                fillOpacity={0.08}
                ifOverflow="hidden"
              />
            )}
            <Tooltip
              contentStyle={CHART_TOOLTIP_STYLE}
              itemStyle={CHART_TOOLTIP_ITEM_STYLE}
              labelStyle={CHART_TOOLTIP_LABEL_STYLE}
              labelFormatter={(label, payload) => {
                const text = typeof label === 'string' ? formatDate(label) : label
                return payload?.[0]?.payload?.reconstructed ? `${text} · replayed` : text
              }}
              formatter={(value: number | undefined) => tooltipValue(value, formatCurrency)}
            />
            <Line
              type="monotone"
              dataKey="value"
              name="Next 12 months"
              stroke="hsl(var(--primary))"
              strokeWidth={2}
              dot={false}
              activeDot={{ r: 5 }}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>

      <div className="space-y-1 text-xs text-muted-foreground">
        {trend.parts.length > 0 && (
          <p>
            Of the change{trend.partsSince ? ` since ${shortDay(trend.partsSince)}` : ''}:{' '}
            {trend.parts.map((part, i) => (
              <span key={part.key}>
                {i > 0 && ' · '}
                <span className="tabular-nums">{signed(part.value)}</span> {partLabel(part)}
              </span>
            ))}
          </p>
        )}
        {perShare && (
          <p>
            Payers&apos; own growth per share over the last year:{' '}
            <span className="tabular-nums">
              {perShare.pct > 0 ? '+' : ''}{perShare.pct.toFixed(1)}%
            </span>
            {' '}(covers {perShare.covered_pct.toFixed(0)}% of the forecast).
          </p>
        )}
        {trend.replayed && (
          <p>
            Shaded: {shortDay(trend.replayed.from)} – {shortDay(trend.replayed.to)} replayed
            under today&apos;s rules, not what was shown on the day.
          </p>
        )}
      </div>
    </section>
  )
}
