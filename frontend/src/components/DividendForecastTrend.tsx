import { useId, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChartSpline, ChevronRight } from 'lucide-react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceDot,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { forecastHistoryQuery } from '@/lib/queryCache'
import { chartColor, NEUTRAL_COLOR } from '@/lib/chartPalette'
import { IconChip } from '@/components/ui/IconChip'
import {
  CHART_TOOLTIP_ITEM_STYLE,
  CHART_TOOLTIP_LABEL_STYLE,
  CHART_TOOLTIP_STYLE,
  CHART_GRID_PROPS,
} from '@/lib/chartTooltip'
import { useCurrencySymbol, useFormatCurrency } from '@/lib/CurrencyContext'
import { dividendAmount } from '@/lib/dividendFormat'
import {
  forecastTrend,
  fromBuysLabel,
  fromRaisesLabel,
  type ForecastTrendMonth,
  type LiveForecast,
} from '@/lib/dividendForecastTrend'
import { dividendMonthLabel } from '@/lib/dividendChart'
import { forecastMilestones } from '@/lib/dividendMilestones'
import { DataTable, type Column } from '@/components/ui/DataTable'
import { axisFloor, niceTicks } from '@/lib/niceTicks'
import { useResizableHeight } from '@/lib/useResizableHeight'
import { useIsCompact } from '@/lib/useMediaQuery'
import { cn, formatAxisMoney, formatDate, formatShortDay, formatSigned, tooltipValue } from '@/lib/utils'
import { DeltaChip } from './DeltaChip'

/**
 * The forecast's change month by month (review item FD1): what each month added, split as
 * the header splits the total. The months add up to the header's since-start figures.
 */
export function forecastMonthColumns(
  formatCurrency: (value: number) => string,
): Column<ForecastTrendMonth>[] {
  const signed = (value: number) => formatSigned(value, formatCurrency)
  return [
    {
      key: 'month', header: 'Month', shortHeader: 'Month', mobile: 'title',
      cell: (m) => dividendMonthLabel(m.month, true),
    },
    {
      key: 'change', header: 'Change', shortHeader: 'Change', align: 'right', mobile: 'value',
      cell: (m) => signed(m.change),
    },
    { key: 'buys', header: 'Buys', shortHeader: 'Buys', align: 'right', cell: (m) => signed(m.buys) },
    {
      key: 'raises', header: 'Raises', shortHeader: 'Raises', align: 'right',
      cell: (m) => signed(m.raises),
    },
    { key: 'other', header: 'Other', shortHeader: 'Other', align: 'right', cell: (m) => signed(m.other) },
  ]
}

/** The chart's height when first opened, and the range a drag may take it to. */
const CHART_HEIGHT = { storageKey: 'dividendForecastTrendHeight', initial: 260, min: 120, max: 560 }

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
 * also carries the month's and the year's growth, what shares bought added — the part
 * of the growth the owner controls — and what the payers' raises added; the other
 * parts stay in the collapsed detail.
 * The received line under the forecast is the trailing twelve months, so the income
 * actually paid can be watched climbing toward what is forecast.
 */
export function DividendForecastTrend({ live }: { live?: LiveForecast | null }) {
  const formatCurrency = useFormatCurrency()
  const curSym = useCurrencySymbol()
  const isCompact = useIsCompact()
  const [open, setOpen] = useState(false)
  const contentId = useId()
  const { height: chartHeight, handleProps: resizeHandle } = useResizableHeight(CHART_HEIGHT)
  const { data } = useQuery(forecastHistoryQuery)
  const trend = useMemo(() => forecastTrend(data, live), [data, live])
  const monthColumns = useMemo(() => forecastMonthColumns(formatCurrency), [formatCurrency])
  // The round levels the line has crossed since the start (review item FD3).
  const milestones = useMemo(() => (trend ? forecastMilestones(trend.points) : null), [trend])

  const axis = useMemo(() => {
    if (!trend) return null
    const values = trend.points.flatMap((p) => [p.value, p.received])
    const min = Math.min(...values)
    const max = Math.max(...values)
    const pad = (max - min || Math.abs(max) || 1) * 0.15
    return niceTicks(min - pad, max + pad, 3, axisFloor(min, min - pad, min - pad))
  }, [trend])

  if (!trend || !axis || trend.points.length < 2) return null

  const signed = (value: number) => formatSigned(value, formatCurrency)

  return (
    <section
      aria-label="Dividend forecast over time"
      className="rounded-xl border border-border/70 bg-raised"
    >
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-controls={contentId}
        className="flex w-full select-none flex-wrap items-center gap-x-3 gap-y-1.5 rounded-xl px-4 py-3 text-left ring-offset-background transition-colors hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
      >
        <ChevronRight
          aria-hidden="true"
          className={cn(
            '-ml-1 h-4 w-4 shrink-0 text-muted-foreground transition-transform duration-200',
            open && 'rotate-90',
          )}
        />
        <IconChip icon={ChartSpline} />
        <span className="text-xs font-medium text-muted-foreground">Next 12 months</span>
        <span className="text-sm font-semibold tabular-nums">
          {dividendAmount(trend.latest, curSym)}
        </span>
        <DeltaChip
          variant="pill"
          pct={trend.sincePct}
          label={`since ${formatShortDay(trend.start)}`}
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
        {trend.fromRaises != null && (
          <span className="text-xs tabular-nums text-muted-foreground">
            {signed(trend.fromRaises)} {fromRaisesLabel(trend.fromRaises)}
          </span>
        )}
      </button>

      {open && (
        <div id={contentId} className="space-y-2 px-4 pb-3">
          <div className="w-full" style={{ height: chartHeight }}>
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={trend.points} margin={{ top: 4, right: isCompact ? 8 : 16, left: 0, bottom: 0 }}>
                <CartesianGrid {...CHART_GRID_PROPS} />
                <XAxis
                  dataKey="date"
                  className="text-xs"
                  tick={{ fill: 'hsl(var(--muted-foreground))' }}
                  tickFormatter={formatShortDay}
                  interval="preserveStartEnd"
                  minTickGap={isCompact ? 44 : 60}
                />
                <YAxis
                  className="text-xs"
                  tick={{ fill: 'hsl(var(--muted-foreground))' }}
                  // The headline carries the currency; on a phone the axis has no room to repeat it.
                  tickFormatter={(value: number) => formatAxisMoney(value, isCompact ? '' : curSym, axis.step)}
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
                  stroke={NEUTRAL_COLOR}
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
                  stroke={chartColor(0)}
                  strokeWidth={2}
                  dot={false}
                  activeDot={{ r: 5 }}
                  isAnimationActive={false}
                />
                {milestones?.crossed.map((c) => (
                  <ReferenceDot
                    key={c.level}
                    x={c.date}
                    y={c.value}
                    r={3.5}
                    fill={chartColor(0)}
                    stroke="hsl(var(--background))"
                    strokeWidth={1.5}
                    label={{
                      value: formatAxisMoney(c.level, curSym, milestones.step),
                      position: 'top',
                      className: 'fill-muted-foreground text-[10px]',
                    }}
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>
          <div
            {...resizeHandle}
            aria-label="Resize chart"
            className="group -mt-1 flex h-3 w-full cursor-row-resize touch-none select-none items-center justify-center rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <span className="h-1 w-10 rounded-full bg-border transition-colors group-hover:bg-muted-foreground/60" />
          </div>

          {trend.parts.length > 0 && (
            <p className="text-xs text-muted-foreground">
              Of the change{trend.partsSince ? ` since ${formatShortDay(trend.partsSince)}` : ''}:{' '}
              {trend.parts.map((part, i) => (
                <span key={part.key}>
                  {i > 0 && ' · '}
                  <span className="tabular-nums">{signed(part.value)}</span> {part.label}
                </span>
              ))}
            </p>
          )}

          {trend.months.length > 1 && (
            <DataTable
              rows={trend.months}
              columns={monthColumns}
              getRowKey={(m) => m.month}
              label="Forecast change by month"
              density="compact"
            />
          )}
        </div>
      )}
    </section>
  )
}
