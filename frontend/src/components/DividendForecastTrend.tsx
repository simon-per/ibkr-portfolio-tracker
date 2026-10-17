import { useId, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronRight } from 'lucide-react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api } from '@/lib/api'
import {
  CHART_TOOLTIP_ITEM_STYLE,
  CHART_TOOLTIP_LABEL_STYLE,
  CHART_TOOLTIP_STYLE, CHART_GRID_PROPS } from '@/lib/chartTooltip'
import { useCurrencySymbol, useFormatCurrency } from '@/lib/CurrencyContext'
import { dividendAmount } from '@/lib/dividendFormat'
import { forecastTrend, fromBuysLabel, type LiveForecast } from '@/lib/dividendForecastTrend'
import { axisFloor, niceTicks } from '@/lib/niceTicks'
import { useIsCompact } from '@/lib/useMediaQuery'
import { cn, formatDate, parseLocalDate, tooltipValue } from '@/lib/utils'
import { DeltaChip } from './DeltaChip'

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
 *
 * Collapsed by default, like the other optional cards: the header row is the toggle and
 * keeps the figure, the growth and its "est." on the surface in both states. Replayed
 * days are drawn like recorded ones — the owner's call (2026-10-14): an estimate is
 * what the whole line is.
 *
 * `live` is the breakdown's own figure for today, so the header and the KPI tile above
 * it print one "Next 12 months" (`lib/dividendForecastTrend.ts`, rule 3). The header
 * also carries the month's and the year's growth and what shares bought added — the
 * part of the growth the owner controls; the other parts stay in the collapsed detail.
 * The received line under the forecast is the trailing twelve months, so the income
 * actually paid can be watched climbing toward what is forecast.
 */
export function DividendForecastTrend({ live }: { live?: LiveForecast | null }) {
  const formatCurrency = useFormatCurrency()
  const curSym = useCurrencySymbol()
  const isCompact = useIsCompact()
  const [open, setOpen] = useState(false)
  const contentId = useId()
  const { data } = useQuery({
    queryKey: ['dividends', 'forecast-history'],
    queryFn: () => api.getDividendForecastHistory(),
  })
  const trend = useMemo(() => forecastTrend(data, live), [data, live])

  const axis = useMemo(() => {
    if (!trend) return null
    const values = trend.points.flatMap((p) => [p.value, p.received])
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
      className="rounded-xl bg-muted/50"
    >
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-controls={contentId}
        className="flex w-full select-none flex-wrap items-center gap-x-3 gap-y-1 rounded-xl px-4 py-3 text-left ring-offset-background transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
      >
        <ChevronRight
          aria-hidden="true"
          className={cn(
            '-ml-1 h-4 w-4 shrink-0 text-muted-foreground transition-transform duration-200',
            open && 'rotate-90',
          )}
        />
        <span className="text-xs font-medium text-muted-foreground">Next 12 months</span>
        <span className="text-sm font-semibold tabular-nums">
          {dividendAmount(trend.latest, curSym)}
        </span>
        <DeltaChip
          pct={trend.sincePct}
          label={`since ${shortDay(trend.start)}`}
          flatBand={0}
          projected
          className="text-sm"
          title="Forecast growth since start"
        />
        {trend.mtdPct != null && (
          <DeltaChip pct={trend.mtdPct} label="MTD" flatBand={0} title="Forecast growth this month" />
        )}
        {trend.ytdPct != null && (
          <DeltaChip pct={trend.ytdPct} label="YTD" flatBand={0} title="Forecast growth this year" />
        )}
        {trend.fromBuys != null && (
          <span className="text-xs tabular-nums text-muted-foreground">
            {signed(trend.fromBuys)} {fromBuysLabel(trend.fromBuys)}
          </span>
        )}
      </button>

      {open && (
        <div id={contentId} className="space-y-2 px-4 pb-3">
          <div className="h-[120px] w-full">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={trend.points} margin={{ top: 4, right: isCompact ? 8 : 16, left: 0, bottom: 0 }}>
                <CartesianGrid {...CHART_GRID_PROPS} />
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
                  // The headline carries the currency; on a phone the axis has no room to repeat it.
                  tickFormatter={(value: number) =>
                    isCompact ? String(Math.round(value)) : `${curSym}${Math.round(value)}`}
                  domain={axis.domain}
                  ticks={axis.ticks}
                  width="auto"
                />
                <Tooltip
                  contentStyle={CHART_TOOLTIP_STYLE}
                  itemStyle={CHART_TOOLTIP_ITEM_STYLE}
                  labelStyle={CHART_TOOLTIP_LABEL_STYLE}
                  labelFormatter={(label) => (typeof label === 'string' ? formatDate(label) : label)}
                  formatter={(value: number | undefined) => tooltipValue(value, formatCurrency)}
                />
                <Line
                  type="monotone"
                  dataKey="received"
                  name="Received"
                  stroke="hsl(var(--muted-foreground))"
                  strokeWidth={1.5}
                  strokeOpacity={0.7}
                  dot={false}
                  activeDot={{ r: 4 }}
                  isAnimationActive={false}
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

          {trend.parts.length > 0 && (
            <p className="text-xs text-muted-foreground">
              Of the change{trend.partsSince ? ` since ${shortDay(trend.partsSince)}` : ''}:{' '}
              {trend.parts.map((part, i) => (
                <span key={part.key}>
                  {i > 0 && ' · '}
                  <span className="tabular-nums">{signed(part.value)}</span> {part.label}
                </span>
              ))}
            </p>
          )}
        </div>
      )}
    </section>
  )
}
