import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { Building2, Maximize2, Minimize2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { SectionHeader } from '@/components/ui/SectionHeader'
import { SegmentedControl } from '@/components/ui/SegmentedControl'
import { TickerAvatar } from '@/components/ui/TickerAvatar'
import type {
  CompanyFinancialsPeriod,
  CompanyFinancialsProfile,
  CompanyFinancialsRange,
  CompanyFinancialsLatestQuarter,
  CompanyFinancialsResponse,
  CompanyFinancialsSeriesKey,
} from '@/lib/api'
import { ratingBadgeClass, ratingCountsText } from '@/lib/analystRating'
import { AnalystRatingBar } from './AnalystRatingBadge'
import { useKeepPreviousData } from '@/lib/CurrencyContext'
import { companyFinancialsCompaniesQuery, companyFinancialsQuery } from '@/lib/queryCache'
import { replaceRoute, useTabRoute } from '@/lib/tabRoute'
import {
  type CompanyBasis,
  type CompanyChartDef,
  type CompanyChartRow,
  type CompanyValueFormat,
  type KeyStatDef,
  type OverviewFigure,
  BASIS_OPTIONS,
  CAGR_SPANS,
  PERIOD_OPTIONS,
  RANGE_OPTIONS,
  axisTicks,
  chartRows,
  chartView,
  companyGroups,
  fiscalYearEndMonth,
  growthOf,
  hasAnyData,
  overviewValue,
  periodText,
  readCompanyPick,
  resolveCompany,
  seriesWithData,
  shortPeriodLabel,
  visibleCharts,
  visibleOverviewGroups,
  writeCompanyPick,
} from '@/lib/companyFinancials'
import { chartColor, LOSS_COLOR } from '@/lib/chartPalette'
import { CHART_GRID_PROPS, CHART_TOOLTIP_LABEL_STYLE, CHART_TOOLTIP_STYLE } from '@/lib/chartTooltip'
import { robustlyCapped, zeroBasedTicks, type Axis } from '@/lib/niceTicks'
import { displayName } from '@/lib/securityName'
import { TONE_CHIP, TONE_TEXT, type Tone } from '@/lib/tone'
import { useIsCompact } from '@/lib/useMediaQuery'
import {
  ABSENT,
  cn,
  currencySymbol,
  formatAxisMoney,
  formatCompactCount,
  formatCompactMoney,
  formatCurrency,
  formatFractionPercent,
  formatMultiple,
  formatSigned,
  placeholderDim,
} from '@/lib/utils'

/** A figure as the card prints it, in the company's reporting currency. */
function valueFormatter(format: CompanyValueFormat, currency: string): (v: number) => string {
  switch (format) {
    case 'money': return (v) => formatCompactMoney(v, currency)
    case 'perShare': return (v) => formatCurrency(v, currency)
    case 'count': return formatCompactCount
    case 'percent': return (v) => formatFractionPercent(v)
    case 'yield': return (v) => formatFractionPercent(v, 2)
    case 'multiple': return formatMultiple
  }
}

/** A tick label, with as many decimals as the axis step needs. */
function tickFormatter(format: CompanyValueFormat, currency: string, axis: Axis): (v: number) => string {
  switch (format) {
    case 'money':
    case 'perShare':
      return (v) => formatAxisMoney(v, currencySymbol(currency), axis.step)
    case 'count':
    case 'multiple':
      return (v) => formatAxisMoney(v, '', axis.step)
    case 'percent':
    case 'yield': {
      const digits = Number.isInteger(Math.round(axis.step * 1e6) / 1e4) ? 0 : 1
      return (v) => formatFractionPercent(v, digits)
    }
  }
}

const signedPercent = (v: number | null) => (v == null ? ABSENT : formatSigned(v, (n) => formatFractionPercent(n)))

/** Ink for a signed growth figure, read off what is printed so "0.0%" is never green. */
function growthTone(printed: string, inverse: boolean): Tone {
  if (printed.startsWith('+')) return inverse ? 'negative' : 'positive'
  if (printed.startsWith('-')) return inverse ? 'positive' : 'negative'
  return 'neutral'
}

/** Today's key figure of the same name as a series (`CompanyChartDef.current`), or null. */
function keyFigure(data: CompanyFinancialsResponse, key: CompanyFinancialsSeriesKey): number | null {
  const stats: Partial<Record<CompanyFinancialsSeriesKey, number | null>> = data.key_stats
  return stats[key] ?? null
}

/** Recharts reports the hovered index as a number or a numeric string, or nothing. */
function toIndex(value: unknown): number | null {
  if (value == null || value === '') return null
  const n = Number(value)
  return Number.isInteger(n) && n >= 0 ? n : null
}

function Stat({ label, value, tone = 'neutral' }: { label: string; value: string; tone?: Tone }) {
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
      <span className="text-muted-foreground">{label}</span>
      <span className={cn('font-medium tabular-nums', value === ABSENT ? 'text-muted-foreground' : TONE_TEXT[tone])}>
        {value}
      </span>
    </span>
  )
}

function ChartTooltip({ active, payload, series, format }: {
  active?: boolean
  payload?: ReadonlyArray<{ payload?: CompanyChartRow }>
  series: { key: CompanyFinancialsSeriesKey; name: string; color: string }[]
  format: (v: number) => string
}) {
  const row = payload?.[0]?.payload
  if (!active || !row) return null
  const rows = series
    .map((s) => ({ ...s, value: row[s.key], yoy: row.yoy[s.key] ?? null }))
    .filter((s): s is typeof s & { value: number } => s.value != null)
  if (rows.length === 0) return null
  const named = series.length > 1
  return (
    <div style={CHART_TOOLTIP_STYLE} className="space-y-0.5 px-2.5 py-2 shadow-md">
      <p style={CHART_TOOLTIP_LABEL_STYLE}>{periodText(row.label)}</p>
      {rows.map((s) => (
        <p key={s.key} className="flex items-center gap-1.5">
          {named && (
            <>
              <span aria-hidden="true" className="h-2 w-2 rounded-sm" style={{ backgroundColor: s.color }} />
              <span className="text-muted-foreground">{s.name}</span>
            </>
          )}
          <span className="font-medium tabular-nums">{format(s.value)}</span>
          {s.yoy != null && <span className="text-muted-foreground tabular-nums">{signedPercent(s.yoy)} YoY</span>}
        </p>
      ))}
    </div>
  )
}

function CompanyChartCard({ chart, basis, data, expanded, onExpand }: {
  chart: CompanyChartDef
  basis: CompanyBasis
  data: CompanyFinancialsResponse
  expanded: boolean
  onExpand: () => void
}) {
  const isCompact = useIsCompact()
  const view = chartView(chart, basis)
  // Series hidden from the legend, and the period under the pointer (null: the newest).
  const [hidden, setHidden] = useState<ReadonlySet<CompanyFinancialsSeriesKey>>(() => new Set())
  const [active, setActive] = useState<number | null>(null)

  const series = seriesWithData(data, view).map((s, i) => ({ ...s, color: chartColor(i) }))
  const kept = series.filter((s) => !hidden.has(s.key))
  // An empty plot answers nothing: if the lines left drawn have no data here, draw them all.
  const shown = kept.length > 0 ? kept : series
  const maxTicks = isCompact ? 6 : expanded ? 10 : 7
  // The rows keep their identity across hovers: a new `data` array would restart the bars'
  // animation under the pointer. Keyed on the props the series come from.
  const rows = useMemo(
    () => chartRows(data, chartView(chart, basis).series.map((s) => s.key)),
    [data, chart, basis],
  )
  // A multiple's axis is cut at a robust bound so one spike cannot flatten the rest; the
  // spike runs off the top and its own figure stays in the tooltip (review item VI5).
  const capped = view.format === 'multiple'
  const plotted = rows.flatMap((r) => shown.map((s) => r[s.key] ?? NaN))
  const axis = zeroBasedTicks(capped ? robustlyCapped(plotted) : plotted, 5)
  const periodTicks = axisTicks(rows, maxTicks)
  const format = valueFormatter(view.format, data.currency)
  const tick = tickFormatter(view.format, data.currency, axis)

  const row: CompanyChartRow | undefined = rows[active ?? rows.length - 1]
  // A `current` card reads today's figures until a period is hovered — the key figures' own
  // P/E, not the last quarter end's — when the close they come from is known and any is.
  const priceDate = data.profile.price_date
  const today =
    chart.current === true && active == null && priceDate != null
    && shown.some((s) => keyFigure(data, s.key) != null)
  const valueOf = (key: CompanyFinancialsSeriesKey) => (today ? keyFigure(data, key) : row?.[key] ?? null)
  // The first drawn series with a value at this period: BKNG tagged gross profit only in its
  // early years, and a dash would head its margins card.
  const lead = shown.find((s) => valueOf(s.key) != null) ?? shown[0] ?? series[0]
  const leadValue = valueOf(lead.key)
  const growth = chart.header === 'growth'
  const inverse = chart.inverse ?? false
  const pointYoy = growth ? signedPercent(row?.yoy[lead.key] ?? null) : null
  const leadCagrs = growthOf(data, lead.key).cagrs
  // A multiple has no growth rate; its own medians are what today's reads against (R3).
  const leadMedians = growthOf(data, lead.key).medians
  const medianChips = view.format === 'multiple'
    ? CAGR_SPANS.flatMap((span) => {
        const value = leadMedians[span.key]
        return value == null ? [] : [{ ...span, printed: format(value) }]
      })
    : []
  const cagrs = growth
    ? CAGR_SPANS.flatMap((span) => {
        const value = leadCagrs[span.key]
        return value == null ? [] : [{ ...span, printed: signedPercent(value) }]
      })
    : []

  const toggle = (key: CompanyFinancialsSeriesKey) => setHidden((prev) => {
    const next = new Set(prev)
    if (next.has(key)) next.delete(key)
    // The last line drawn stays drawn: an empty plot answers nothing.
    else if (shown.length > 1) next.add(key)
    return next
  })

  const hover = {
    onMouseMove: (state: { activeTooltipIndex?: unknown }) => setActive(toIndex(state?.activeTooltipIndex)),
    onMouseLeave: () => setActive(null),
  }
  const xAxis = (
    <XAxis
      dataKey="label"
      tick={{ fill: 'hsl(var(--muted-foreground))' }}
      ticks={periodTicks}
      interval={0}
      tickFormatter={shortPeriodLabel}
    />
  )
  const yAxis = (
    <YAxis
      tick={{ fill: 'hsl(var(--muted-foreground))' }}
      width="auto"
      domain={axis.domain}
      ticks={axis.ticks}
      tickFormatter={tick}
      allowDataOverflow={capped}
    />
  )
  const zeroLine = axis.domain[0] < 0 && (
    <ReferenceLine y={0} stroke="hsl(var(--muted-foreground))" strokeWidth={1} />
  )
  const tooltip = (
    <Tooltip
      content={<ChartTooltip series={shown} format={format} />}
      cursor={chart.kind === 'bar' ? { fill: 'hsl(var(--muted))', opacity: 0.5 } : { stroke: 'hsl(var(--muted-foreground))' }}
    />
  )
  // The right margin leaves room for the newest period's label, centred on the last bar or
  // on a line's last point at the very edge: about half its width, so the price card's
  // "Nov '26" is not cut where "FY25" fitted in 16px.
  const lastTick = periodTicks.at(-1)
  const right = Math.max(16, Math.ceil((lastTick ? shortPeriodLabel(lastTick).length : 0) * 3.6))
  const margin = { top: 8, right, left: 0, bottom: 0 }
  const plotHeight = isCompact ? 'h-[260px]' : expanded ? 'h-[460px]' : 'h-[320px]'

  return (
    <Card data-chart={chart.id} className={cn('flex min-w-0 flex-col', expanded && 'lg:col-span-2')}>
      <SectionHeader
        title={view.title}
        className="pb-1 [&_h3]:text-base"
        aside={!isCompact && (
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8 text-muted-foreground"
            aria-label={expanded ? `Shrink ${view.title}` : `Expand ${view.title}`}
            aria-pressed={expanded}
            onClick={onExpand}
          >
            {expanded ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
          </Button>
        )}
      />
      <CardContent className="flex flex-1 flex-col gap-3 pt-0">
        <div className="flex flex-wrap items-end justify-between gap-x-4 gap-y-1">
          <div className="min-w-0">
            <p className="text-2xl font-semibold tabular-nums leading-tight" data-testid="headline">
              {leadValue == null ? ABSENT : format(leadValue)}
            </p>
            <p className="flex flex-wrap items-center gap-x-3 text-xs text-muted-foreground">
              <span className="tabular-nums">
                {today && priceDate ? periodText(priceDate) : row ? periodText(row.label) : ABSENT}
              </span>
              {pointYoy != null && <Stat label="YoY" value={pointYoy} tone={growthTone(pointYoy, inverse)} />}
            </p>
          </div>
          {medianChips.length > 0 && (
            <span className="flex flex-wrap items-center gap-x-3 text-xs" data-testid="medians">
              <span className="text-muted-foreground">Median</span>
              {medianChips.map((c) => (
                <Stat key={c.key} label={c.label} value={c.printed} />
              ))}
            </span>
          )}
          {cagrs.length > 0 && (
            <span className="flex flex-wrap items-center gap-x-3 text-xs" data-testid="cagrs">
              <span className="text-muted-foreground">CAGR</span>
              {cagrs.map((c) => (
                <Stat key={c.key} label={c.label} value={c.printed} tone={growthTone(c.printed, inverse)} />
              ))}
            </span>
          )}
        </div>
        {series.length > 1 && (
          <SegmentedControl
            variant="buttons"
            label={`${view.title} series`}
            className="flex-wrap"
            buttonClassName="h-7 gap-1.5 px-2 text-xs"
            isSelected={(key) => !hidden.has(key)}
            onToggle={toggle}
            options={series.map((s) => {
              const off = hidden.has(s.key)
              const v = valueOf(s.key)
              return {
                value: s.key,
                label: (
                  <>
                    <span aria-hidden="true" className="h-2 w-2 rounded-sm" style={{ backgroundColor: off ? 'transparent' : s.color, outline: `1px solid ${s.color}` }} />
                    <span>{s.name}</span>
                    <span className="font-medium tabular-nums text-foreground">{v == null ? ABSENT : format(v)}</span>
                  </>
                ),
              }
            })}
          />
        )}
        <div className={cn('mt-auto w-full', plotHeight)}>
          <ResponsiveContainer width="100%" height="100%">
            {chart.kind === 'line' ? (
              <LineChart data={rows} margin={margin} {...hover}>
                <CartesianGrid {...CHART_GRID_PROPS} />
                {xAxis}
                {yAxis}
                {tooltip}
                {zeroLine}
                {shown.map((s) => (
                  <Line
                    key={s.key}
                    type="monotone"
                    dataKey={s.key}
                    name={s.name}
                    stroke={s.color}
                    strokeWidth={2}
                    dot={false}
                    activeDot={{ r: 4 }}
                    connectNulls={false}
                    animationDuration={400}
                  />
                ))}
              </LineChart>
            ) : (
              <BarChart data={rows} margin={margin} barGap={1} {...hover}>
                <CartesianGrid {...CHART_GRID_PROPS} />
                {xAxis}
                {yAxis}
                {tooltip}
                {zeroLine}
                {shown.map((s) => (
                  <Bar key={s.key} dataKey={s.key} name={s.name} fill={s.color} maxBarSize={36} radius={[3, 3, 0, 0]} animationDuration={400}>
                    {/* One series: a loss is drawn in the loss colour. Hovering dims the other periods. */}
                    {rows.map((r, i) => (
                      <Cell
                        key={r.period_end}
                        fill={shown.length === 1 && (r[s.key] ?? 0) < 0 ? LOSS_COLOR : s.color}
                        fillOpacity={active == null || active === i ? 1 : 0.45}
                      />
                    ))}
                  </Bar>
                ))}
              </BarChart>
            )}
          </ResponsiveContainer>
        </div>
      </CardContent>
    </Card>
  )
}

/** A key stat's figure: a growth rate signed and inked, anything else as its cards print it. */
function keyStatText(def: Pick<KeyStatDef, 'format'>, value: number, currency: string): string {
  return def.format === 'signed' ? signedPercent(value) : valueFormatter(def.format, currency)(value)
}

/** The price line's changes, shortest first; the multi-year ones are annualised. */
const RETURNS: readonly { key: 'return_1m' | 'return_1y' | 'return_5y' | 'return_10y'; label: string }[] = [
  { key: 'return_1m', label: '1M' },
  { key: 'return_1y', label: '1Y' },
  { key: 'return_5y', label: '5Y p.a.' },
  { key: 'return_10y', label: '10Y p.a.' },
]

/** The 52-week range of the closes, with the newest close marked on it. */
function RangeBar({ low, high, price, format }: {
  low: number | null
  high: number | null
  price: number
  format: (v: number) => string
}) {
  if (low == null || high == null || high <= low) return null
  const at = Math.min(1, Math.max(0, (price - low) / (high - low)))
  return (
    <span className="inline-flex items-center gap-2 text-xs tabular-nums text-muted-foreground" data-testid="range-52w">
      <span>52W</span>
      <span>{format(low)}</span>
      <span aria-hidden="true" className="relative h-1.5 w-20 rounded-full bg-muted sm:w-28">
        <span className="absolute top-1/2 h-3 w-1.5 -translate-x-1/2 -translate-y-1/2 rounded-sm bg-primary" style={{ left: `${at * 100}%` }} />
      </span>
      <span>{format(high)}</span>
    </span>
  )
}

/** What the company does: two lines, the rest a click away. */
function Description({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  return (
    <button
      type="button"
      aria-expanded={open}
      onClick={() => setOpen((v) => !v)}
      data-testid="company-description"
      className={cn(
        'block w-full rounded-sm text-left text-sm text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        !open && 'line-clamp-2',
      )}
    >
      {text}
    </button>
  )
}

/** One figure of a group: its label, its value, and the note beside it (YoY, payout). */
function FigureRow({ figure, data }: { figure: OverviewFigure; data: CompanyFinancialsResponse }) {
  const value = overviewValue(figure, data.key_stats, data.profile) as number
  const def = figure.source === 'stats' ? figure.def : figure
  const label = figure.source === 'stats' ? figure.def.label : figure.label
  const inverse = figure.source === 'stats' ? figure.def.inverse ?? false : false
  const printed = keyStatText(def, value, data.currency)
  const note = figure.source === 'stats' ? figure.def.note : undefined
  const noteValue = note ? data.key_stats[note.key] : null
  const notePrinted = note && noteValue != null ? keyStatText(note, noteValue, data.currency) : null
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-2 text-sm">
      <dt className="min-w-0 text-muted-foreground">{label}</dt>
      <dd className="ml-auto text-right">
        <span className={cn('font-medium tabular-nums', def.format === 'signed' && TONE_TEXT[growthTone(printed, inverse)])}>
          {printed}
        </span>
        {note && notePrinted != null && (
          <span className="ml-1.5 text-xs">
            <Stat
              label={note.label}
              value={notePrinted}
              tone={note.format === 'signed' ? growthTone(notePrinted, inverse) : 'neutral'}
            />
          </span>
        )}
      </dd>
    </div>
  )
}

/** The Street's view and the next report: each item only when Yahoo had it. */
function StreetRow({ profile }: { profile: CompanyFinancialsProfile }) {
  const money = (v: number) => (profile.yahoo_currency ? formatCurrency(v, profile.yahoo_currency) : formatMultiple(v))
  const upside = profile.target_upside != null ? signedPercent(profile.target_upside) : null
  const items = [
    // The full breakdown here: the chip, its bar and every count written out. The counts
    // are the analysts, so Yahoo's separate target-analyst count is not repeated.
    profile.rating && (
      <span key="rating" className="inline-flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className={cn('rounded-full px-2 py-0.5 text-xs font-medium', ratingBadgeClass(profile.rating.consensus))}>
          {profile.rating.consensus}
        </span>
        <AnalystRatingBar counts={profile.rating} className="w-24" />
        <span className="text-muted-foreground tabular-nums">{ratingCountsText(profile.rating)}</span>
      </span>
    ),
    profile.target_mean != null && (
      <span key="target" className="inline-flex flex-wrap items-baseline gap-x-1.5">
        <span className="text-muted-foreground">Target</span>
        <span className="font-medium tabular-nums">{money(profile.target_mean)}</span>
        {upside && <span className={cn('tabular-nums', TONE_TEXT[growthTone(upside, false)])}>{upside}</span>}
        {profile.target_low != null && profile.target_high != null && (
          <span className="text-xs tabular-nums text-muted-foreground">
            {money(profile.target_low)}–{money(profile.target_high)}
          </span>
        )}
      </span>
    ),
    profile.next_earnings_date && (
      <span key="earnings" className="inline-flex flex-wrap items-baseline gap-x-1.5">
        <span className="text-muted-foreground">Earnings</span>
        <span className="font-medium tabular-nums">{periodText(profile.next_earnings_date.slice(0, 10))}</span>
        {profile.next_eps_estimate != null && (
          <span className="text-xs tabular-nums text-muted-foreground">EPS est. {money(profile.next_eps_estimate)}</span>
        )}
      </span>
    ),
  ].filter(Boolean)
  if (items.length === 0) return null
  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-2 border-t pt-3 text-sm" data-testid="street-row">
      {items}
    </div>
  )
}

/**
 * The company itself, at a glance (docs/frontend.md, *The Company tab*): its industry and
 * fiscal year, its price and how it moved, what it does, its figures grouped by meaning,
 * and what the Street expects. No portfolio figure: this tab is about the company.
 */
function CompanyOverview({ data }: { data: CompanyFinancialsResponse }) {
  const profile = data.profile
  const fyMonth = fiscalYearEndMonth(profile.fiscal_year_end)
  const tags = [profile.industry, fyMonth && `FY ends ${fyMonth}`].filter(Boolean)
  const price = (v: number) => formatCurrency(v, data.currency)
  const groups = visibleOverviewGroups(data.key_stats, profile)
  const returns = RETURNS.flatMap((r) => {
    const v = profile[r.key]
    return v == null ? [] : [{ ...r, printed: signedPercent(v) }]
  })
  return (
    <div className="space-y-4" data-testid="company-overview">
      {tags.length > 0 && <p className="text-sm text-muted-foreground" data-testid="company-tags">{tags.join(' · ')}</p>}
      {profile.price != null && (
        <div className="flex flex-wrap items-end gap-x-6 gap-y-2" data-testid="price-line">
          <div>
            <p className="text-2xl font-semibold tabular-nums leading-tight">{price(profile.price)}</p>
            {profile.price_date && (
              <p className="text-xs tabular-nums text-muted-foreground">{periodText(profile.price_date)}</p>
            )}
          </div>
          {returns.length > 0 && (
            <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
              {returns.map((r) => (
                <Stat key={r.key} label={r.label} value={r.printed} tone={growthTone(r.printed, false)} />
              ))}
            </span>
          )}
          <RangeBar low={profile.week52_low} high={profile.week52_high} price={profile.price} format={price} />
        </div>
      )}
      {profile.description && <Description key={data.cik} text={profile.description} />}
      {groups.length > 0 && (
        <div className="space-y-2" data-testid="overview-figures">
          {data.key_stats.label && (
            <p className="text-xs tabular-nums text-muted-foreground">TTM · {data.key_stats.label}</p>
          )}
          <div className="grid grid-cols-1 gap-x-8 gap-y-5 min-[440px]:grid-cols-2 lg:grid-cols-4">
            {groups.map((group) => (
              <section key={group.title} aria-label={group.title} className="min-w-0">
                <h4 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{group.title}</h4>
                <dl className="mt-2 space-y-1.5">
                  {group.figures.map((figure) => (
                    <FigureRow
                      key={figure.source === 'stats' ? figure.def.key : figure.key}
                      figure={figure}
                      data={data}
                    />
                  ))}
                </dl>
              </section>
            ))}
          </div>
        </div>
      )}
      <LatestQuarter quarter={data.latest_quarter ?? null} />
      <StreetRow profile={profile} />
    </div>
  )
}

/** How long a filing is `new` on the overview. */
const NEW_FILING_DAYS = 7

/**
 * The newest quarter on its own (review item R4): what the latest report said, which the
 * TTM figures blend into three older quarters. Each figure against the same quarter a
 * year before; the margin's change in points. `new` for a week after the filing.
 */
function LatestQuarter({ quarter }: { quarter: CompanyFinancialsLatestQuarter | null }) {
  // The moment the row was first drawn: a render must not read the clock.
  const [now] = useState(() => Date.now())
  if (!quarter) return null
  const margin = quarter.operating_margin_change
  const figures = [
    { label: 'Revenue', value: quarter.revenue_yoy == null ? null : signedPercent(quarter.revenue_yoy) },
    {
      label: 'Op margin',
      value: margin == null ? null : `${margin >= 0 ? '+' : '−'}${Math.abs(margin * 100).toFixed(1)} pts`,
    },
    { label: 'EPS', value: quarter.eps_yoy == null ? null : signedPercent(quarter.eps_yoy) },
    { label: 'FCF', value: quarter.fcf_yoy == null ? null : signedPercent(quarter.fcf_yoy) },
  ].filter((f): f is { label: string; value: string } => f.value != null)
  if (figures.length === 0) return null
  const filed = quarter.filed ? new Date(`${quarter.filed}T00:00:00`) : null
  const fresh = filed != null && (now - filed.getTime()) / 86_400_000 <= NEW_FILING_DAYS
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs" data-testid="latest-quarter">
      <span className="tabular-nums text-muted-foreground">
        Latest quarter · {quarter.label}
        {quarter.filed && <> · filed {periodText(quarter.filed)}</>}
      </span>
      {fresh && (
        <span className={cn('rounded-full px-1.5 py-0.5 text-[10px] font-semibold', TONE_CHIP.positive)}>new</span>
      )}
      {figures.map((f) => (
        <Stat key={f.label} label={f.label} value={f.value} tone={growthTone(f.value, false)} />
      ))}
    </div>
  )
}

/** The overview while its first company loads. */
function OverviewSkeleton() {
  return (
    <div className="space-y-3" aria-hidden="true">
      <div className="h-4 w-56 animate-pulse rounded bg-muted" />
      <div className="h-8 w-40 animate-pulse rounded bg-muted" />
      <div className="h-24 animate-pulse rounded bg-muted" />
    </div>
  )
}

function Notice({ children }: { children: React.ReactNode }) {
  return (
    <div
      role="alert"
      className="rounded-md border border-yellow-600/40 bg-yellow-600/10 px-3 py-2 text-xs text-yellow-700 dark:text-yellow-500"
    >
      {children}
    </div>
  )
}

function ChartSkeletons() {
  return (
    <>
      {Array.from({ length: 6 }, (_, i) => (
        <Card key={i} className="min-w-0">
          <div className="space-y-3 p-[var(--card-padding)]">
            <div className="h-4 w-24 animate-pulse rounded bg-muted" />
            <div className="h-8 w-32 animate-pulse rounded bg-muted" />
            <div className="h-[320px] w-full animate-pulse rounded-md bg-muted" />
          </div>
        </Card>
      ))}
    </>
  )
}

/**
 * One company's fundamentals history from its SEC filings, a chart per figure.
 *
 * The browser only draws: TTM, CAGR, YoY (the series' and each point's), margins and
 * per-share figures are the server's (`/api/company-financials/{id}`), so the period and
 * range toggles are query parameters and Total | Per share only picks which served series a
 * card draws. See docs/frontend.md, *The Company tab*.
 */
export function CompanyTab() {
  const keepPrevious = useKeepPreviousData()
  // A company in the URL (`lib/tabRoute.ts`: a holding's link, or a reload) wins over this
  // browser's last pick; one the list no longer has falls through like a stale pick.
  const route = useTabRoute()
  const linked = route.tab === 'company' ? route.cik : null
  const [picked, setPicked] = useState<number | null>(null)
  const [stored] = useState(readCompanyPick)
  const [period, setPeriod] = useState<CompanyFinancialsPeriod>('ttm')
  const [range, setRange] = useState<CompanyFinancialsRange>('10y')
  const [basis, setBasis] = useState<CompanyBasis>('total')
  const [expanded, setExpanded] = useState<string | null>(null)

  const companies = useQuery(companyFinancialsCompaniesQuery)
  const company = companies.data ? resolveCompany(companies.data, linked ?? picked, stored) : null
  const shownCik = company?.cik ?? null

  // A company opened by link becomes the remembered pick, so the tab's own trigger
  // reopens it rather than the one picked before the link was followed.
  useEffect(() => {
    if (linked != null && shownCik === linked) writeCompanyPick(linked)
  }, [linked, shownCik])

  const financials = useQuery({
    ...companyFinancialsQuery(company?.cik ?? -1, period, range),
    enabled: company != null,
    // The last company's charts stay, dimmed, while the next loads: a skeleton would unmount
    // the toggle just pressed (#68).
    placeholderData: keepPrevious,
  })
  const data = financials.data

  const pick = (cik: number) => {
    setPicked(cik)
    writeCompanyPick(cik)
    // Into the URL too, without a history entry: a reload keeps the company.
    replaceRoute({ tab: 'company', cik })
  }

  if (companies.isError) {
    return (
      <Card>
        <SectionHeader title="Company" icon={Building2} iconTone="muted" />
        <CardContent>
          <p className="py-8 text-center text-sm text-muted-foreground">
            Couldn't load the companies — the server didn't respond. It retries automatically.
          </p>
        </CardContent>
      </Card>
    )
  }

  if (companies.data && companies.data.length === 0) {
    return (
      <Card>
        <SectionHeader title="Company" icon={Building2} iconTone="muted" />
        <CardContent>
          <p className="py-8 text-center text-sm text-muted-foreground">No companies with SEC filings stored.</p>
        </CardContent>
      </Card>
    )
  }

  const charts = data ? visibleCharts(data, basis) : []

  return (
    <div className="space-y-4">
      <Card>
        <SectionHeader
          title={
            <span className="flex min-w-0 items-center gap-2">
              {company && (
                <TickerAvatar symbol={company.symbol} securityId={company.security_id ?? undefined} isin={company.isin} />
              )}
              {companies.data ? (
                <select
                  aria-label="Company"
                  value={company?.cik ?? ''}
                  onChange={(e) => pick(Number(e.target.value))}
                  className="h-9 min-w-0 max-w-full rounded-lg border border-border bg-card px-2.5 text-sm font-medium transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {companyGroups(companies.data).map((group) => (
                    <optgroup key={group.label} label={group.label}>
                      {group.companies.map((c) => (
                        <option key={c.cik} value={c.cik}>
                          {c.symbol} · {displayName(c.name)}
                        </option>
                      ))}
                    </optgroup>
                  ))}
                </select>
              ) : (
                <span className="h-9 w-48 animate-pulse rounded-lg bg-muted" />
              )}
            </span>
          }
          aside={
            <>
              <SegmentedControl label="Basis" value={basis} onChange={setBasis} options={BASIS_OPTIONS} />
              <SegmentedControl label="Period" value={period} onChange={setPeriod} options={PERIOD_OPTIONS} />
              <SegmentedControl label="Range" value={range} onChange={setRange} options={RANGE_OPTIONS} />
            </>
          }
        />
        {/* The overview is the card's body: one card names the company and says what it is. */}
        {!financials.isError && (
          <CardContent
            aria-busy={financials.isPlaceholderData || undefined}
            className={cn('pt-0', placeholderDim(financials.isPlaceholderData))}
          >
            {data ? <CompanyOverview data={data} /> : <OverviewSkeleton />}
          </CardContent>
        )}
      </Card>

      {financials.isError ? (
        <Card>
          <CardContent className="pt-[var(--card-padding)]">
            <p className="py-8 text-center text-sm text-muted-foreground">
              Couldn't load {company?.symbol ?? 'this company'}'s financials — the server didn't respond.
            </p>
          </CardContent>
        </Card>
      ) : (
        <div
          aria-busy={financials.isPlaceholderData || undefined}
          className={cn('space-y-4', placeholderDim(financials.isPlaceholderData))}
        >
          {data?.warnings.map((w, i) => <Notice key={i}>{w}</Notice>)}
          {data && !hasAnyData(data) ? (
            <Notice>No financial data for {data.symbol}.</Notice>
          ) : (
            <>
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              {data
                ? charts.map((chart) => (
                    <CompanyChartCard
                      // A new company or basis is a new set of series: the legend's hidden
                      // lines and the hovered period reset (review item U4).
                      key={`${data.cik}:${chart.id}:${basis}`}
                      chart={chart}
                      basis={basis}
                      data={data}
                      expanded={expanded === chart.id}
                      onExpand={() => setExpanded((e) => (e === chart.id ? null : chart.id))}
                    />
                  ))
                : <ChartSkeletons />}
            </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}
