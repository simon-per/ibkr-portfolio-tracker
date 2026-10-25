import { useMemo, useState } from 'react'
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
  CompanyFinancialsRange,
  CompanyFinancialsResponse,
  CompanyFinancialsSeriesKey,
} from '@/lib/api'
import { useKeepPreviousData } from '@/lib/CurrencyContext'
import { companyFinancialsCompaniesQuery, companyFinancialsQuery } from '@/lib/queryCache'
import {
  type CompanyBasis,
  type CompanyChartDef,
  type CompanyChartRow,
  type CompanyValueFormat,
  type KeyStatDef,
  BASIS_OPTIONS,
  CAGR_SPANS,
  PERIOD_OPTIONS,
  RANGE_OPTIONS,
  axisTicks,
  chartRows,
  chartView,
  companyGroups,
  growthOf,
  hasAnyData,
  readCompanyPick,
  resolveCompany,
  seriesWithData,
  shortPeriodLabel,
  visibleCharts,
  visibleKeyStats,
  writeCompanyPick,
} from '@/lib/companyFinancials'
import { chartColor, LOSS_COLOR } from '@/lib/chartPalette'
import { CHART_GRID_PROPS, CHART_TOOLTIP_LABEL_STYLE, CHART_TOOLTIP_STYLE } from '@/lib/chartTooltip'
import { zeroBasedTicks, type Axis } from '@/lib/niceTicks'
import { displayName } from '@/lib/securityName'
import { TONE_TEXT, type Tone } from '@/lib/tone'
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
  }
}

/** A tick label, with as many decimals as the axis step needs. */
function tickFormatter(format: CompanyValueFormat, currency: string, axis: Axis): (v: number) => string {
  switch (format) {
    case 'money':
    case 'perShare':
      return (v) => formatAxisMoney(v, currencySymbol(currency), axis.step)
    case 'count':
      return (v) => formatAxisMoney(v, '', axis.step)
    case 'percent': {
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
      <p style={CHART_TOOLTIP_LABEL_STYLE}>{row.label}</p>
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
  const shown = series.filter((s) => !hidden.has(s.key))
  const maxTicks = isCompact ? 6 : expanded ? 10 : 7
  // The rows keep their identity across hovers: a new `data` array would restart the bars'
  // animation under the pointer. Keyed on the props the series come from.
  const rows = useMemo(
    () => chartRows(data, chartView(chart, basis).series.map((s) => s.key)),
    [data, chart, basis],
  )
  const axis = zeroBasedTicks(rows.flatMap((r) => shown.map((s) => r[s.key] ?? NaN)), 5)
  const periodTicks = axisTicks(rows, maxTicks)
  const format = valueFormatter(view.format, data.currency)
  const tick = tickFormatter(view.format, data.currency, axis)

  const row: CompanyChartRow | undefined = rows[active ?? rows.length - 1]
  // The first drawn series with a value at this period: BKNG tagged gross profit only in its
  // early years, and a dash would head its margins card.
  const lead = shown.find((s) => row?.[s.key] != null) ?? shown[0] ?? series[0]
  const leadValue = row?.[lead.key] ?? null
  const growth = chart.header === 'growth'
  const inverse = chart.inverse ?? false
  const pointYoy = growth ? signedPercent(row?.yoy[lead.key] ?? null) : null
  const leadCagrs = growthOf(data, lead.key).cagrs
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
  // The right margin leaves room for the newest period's label, centred on the last bar.
  const margin = { top: 8, right: 16, left: 0, bottom: 0 }
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
              <span className="tabular-nums">{row?.label ?? ABSENT}</span>
              {pointYoy != null && <Stat label="YoY" value={pointYoy} tone={growthTone(pointYoy, inverse)} />}
            </p>
          </div>
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
              const v = row?.[s.key] ?? null
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
  if (def.format === 'signed') return signedPercent(value)
  if (def.format === 'multiple') return formatMultiple(value)
  return valueFormatter(def.format, currency)(value)
}

/** The newest TTM point's figures, whatever period the charts below show. */
function KeyStats({ data }: { data: CompanyFinancialsResponse }) {
  const stats = data.key_stats
  const defs = visibleKeyStats(stats)
  if (defs.length === 0) return null
  return (
    <Card data-testid="key-stats">
      <SectionHeader
        title="Key figures"
        className="pb-1 [&_h3]:text-base"
        aside={<span className="text-xs tabular-nums text-muted-foreground">TTM · {stats.label}</span>}
      />
      <CardContent className="pt-0">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3 lg:grid-cols-5">
          {defs.map((def) => {
            const value = stats[def.key] as number
            const printed = keyStatText(def, value, data.currency)
            const noteValue = def.note ? stats[def.note.key] : null
            const notePrinted = def.note && noteValue != null ? keyStatText(def.note, noteValue, data.currency) : null
            return (
              <div key={def.key} className="min-w-0">
                <dt className="truncate text-xs text-muted-foreground">{def.label}</dt>
                <dd
                  className={cn(
                    'text-lg font-semibold tabular-nums leading-tight',
                    def.format === 'signed' && TONE_TEXT[growthTone(printed, def.inverse ?? false)],
                  )}
                >
                  {printed}
                </dd>
                {def.note && notePrinted != null && (
                  <dd className="text-xs">
                    <Stat
                      label={def.note.label}
                      value={notePrinted}
                      tone={def.note.format === 'signed' ? growthTone(notePrinted, def.inverse ?? false) : 'neutral'}
                    />
                  </dd>
                )}
              </div>
            )
          })}
        </dl>
      </CardContent>
    </Card>
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
  const [picked, setPicked] = useState<number | null>(null)
  const [stored] = useState(readCompanyPick)
  const [period, setPeriod] = useState<CompanyFinancialsPeriod>('ttm')
  const [range, setRange] = useState<CompanyFinancialsRange>('10y')
  const [basis, setBasis] = useState<CompanyBasis>('total')
  const [expanded, setExpanded] = useState<string | null>(null)

  const companies = useQuery(companyFinancialsCompaniesQuery)
  const company = companies.data ? resolveCompany(companies.data, picked, stored) : null

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
            {data && <KeyStats data={data} />}
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              {data
                ? charts.map((chart) => (
                    <CompanyChartCard
                      // A new basis is a new set of series: the legend's hidden lines reset.
                      key={`${chart.id}:${basis}`}
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
