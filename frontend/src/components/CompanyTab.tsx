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
import { Building2 } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { SectionHeader } from '@/components/ui/SectionHeader'
import { SegmentedControl } from '@/components/ui/SegmentedControl'
import { TickerAvatar } from '@/components/ui/TickerAvatar'
import type {
  CompanyFinancialsPeriod,
  CompanyFinancialsRange,
  CompanyFinancialsResponse,
} from '@/lib/api'
import { useKeepPreviousData } from '@/lib/CurrencyContext'
import { companyFinancialsCompaniesQuery, companyFinancialsQuery } from '@/lib/queryCache'
import {
  type CompanyChartDef,
  type CompanyChartRow,
  type CompanyValueFormat,
  PERIOD_OPTIONS,
  RANGE_OPTIONS,
  axisTicks,
  chartRows,
  growthOf,
  hasAnyData,
  latestOf,
  readCompanyPick,
  resolveCompany,
  shortPeriodLabel,
  visibleCharts,
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

/** Ink for a signed growth figure, read off what is printed so "0.0%" is never green. */
function growthTone(printed: string, inverse: boolean): Tone {
  if (printed.startsWith('+')) return inverse ? 'negative' : 'positive'
  if (printed.startsWith('-')) return inverse ? 'positive' : 'negative'
  return 'neutral'
}

/** Fewer shares is the shareholder's gain, so the shares card reads growth the other way. */
const INVERSE_GROWTH = new Set(['shares'])

function Stat({ label, value, tone = 'neutral', swatch }: {
  label: string
  value: string
  tone?: Tone
  swatch?: string
}) {
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
      {swatch && <span aria-hidden="true" className="h-2 w-2 shrink-0 rounded-sm" style={{ backgroundColor: swatch }} />}
      <span className="text-muted-foreground">{label}</span>
      <span className={cn('font-medium tabular-nums', value === ABSENT ? 'text-muted-foreground' : TONE_TEXT[tone])}>
        {value}
      </span>
    </span>
  )
}

function ChartTooltip({ active, payload, chart, format }: {
  active?: boolean
  payload?: ReadonlyArray<{ payload?: CompanyChartRow }>
  chart: CompanyChartDef
  format: (v: number) => string
}) {
  const row = payload?.[0]?.payload
  if (!active || !row) return null
  const rows = chart.series
    .map((s, i) => ({ ...s, i, value: row[s.key] }))
    .filter((s): s is typeof s & { value: number } => s.value != null)
  if (rows.length === 0) return null
  const named = chart.series.length > 1
  return (
    <div style={CHART_TOOLTIP_STYLE} className="space-y-0.5 px-2.5 py-2 shadow-md">
      <p style={CHART_TOOLTIP_LABEL_STYLE}>{row.label}</p>
      {rows.map((s) => (
        <p key={s.key} className="flex items-center gap-1.5">
          {named && (
            <>
              <span aria-hidden="true" className="h-2 w-2 rounded-sm" style={{ backgroundColor: chartColor(s.i) }} />
              <span className="text-muted-foreground">{s.name}</span>
            </>
          )}
          <span className="font-medium tabular-nums">{format(s.value)}</span>
        </p>
      ))}
    </div>
  )
}

function CompanyChartCard({ chart, data }: { chart: CompanyChartDef; data: CompanyFinancialsResponse }) {
  const isCompact = useIsCompact()
  const maxTicks = isCompact ? 6 : 5
  const { rows, axis, periodTicks } = useMemo(() => {
    const keys = chart.series.map((s) => s.key)
    const rows = chartRows(data, keys)
    return {
      rows,
      axis: zeroBasedTicks(rows.flatMap((r) => keys.map((k) => r[k] ?? NaN)), 4),
      periodTicks: axisTicks(rows, maxTicks),
    }
  }, [data, chart, maxTicks])
  const format = valueFormatter(chart.format, data.currency)
  const tick = tickFormatter(chart.format, data.currency, axis)

  let stats: React.ReactNode
  let legend: React.ReactNode = null
  if (chart.header === 'latest') {
    stats = chart.series.map((s, i) => {
      const v = latestOf(data, s.key)
      return <Stat key={s.key} label={s.name} value={v == null ? ABSENT : format(v)} swatch={chartColor(i)} />
    })
  } else {
    const { cagr, yoy } = growthOf(data, chart.series[0].key)
    const inverse = INVERSE_GROWTH.has(chart.id)
    const signed = (v: number | null) => (v == null ? ABSENT : formatSigned(v, (n) => formatFractionPercent(n)))
    const cagrText = signed(cagr)
    const yoyText = signed(yoy)
    stats = (
      <>
        <Stat label="CAGR" value={cagrText} tone={growthTone(cagrText, inverse)} />
        <Stat label="YoY" value={yoyText} tone={growthTone(yoyText, inverse)} />
      </>
    )
    if (chart.series.length > 1) {
      legend = chart.series.map((s, i) => (
        <span key={s.key} className="inline-flex items-center gap-1.5 whitespace-nowrap">
          <span aria-hidden="true" className="h-2 w-2 rounded-sm" style={{ backgroundColor: chartColor(i) }} />
          {s.name}
        </span>
      ))
    }
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
  const tooltip = <Tooltip content={<ChartTooltip chart={chart} format={format} />} />
  // The right margin leaves room for the newest period's label, centred on the last bar.
  const margin = { top: 5, right: 16, left: 0, bottom: 0 }

  return (
    // A column with the plot pushed to the bottom: a header that wraps to a second line (the
    // legend, three margins) leaves the plots of one row level with each other.
    <Card data-chart={chart.id} className="flex min-w-0 flex-col">
      <SectionHeader
        title={chart.title}
        className="pb-2 [&_h3]:text-base"
        description={
          <span className="flex flex-col gap-1 text-xs">
            <span className="flex flex-wrap gap-x-3 gap-y-1">{stats}</span>
            {legend && <span className="flex flex-wrap gap-x-3 gap-y-1">{legend}</span>}
          </span>
        }
      />
      <CardContent className="mt-auto pt-0">
        <div className="h-[190px] w-full">
          <ResponsiveContainer width="100%" height="100%">
            {chart.kind === 'line' ? (
              <LineChart data={rows} margin={margin}>
                <CartesianGrid {...CHART_GRID_PROPS} />
                {xAxis}
                {yAxis}
                {tooltip}
                {zeroLine}
                {chart.series.map((s, i) => (
                  <Line
                    key={s.key}
                    type="monotone"
                    dataKey={s.key}
                    name={s.name}
                    stroke={chartColor(i)}
                    strokeWidth={2}
                    dot={false}
                    connectNulls={false}
                  />
                ))}
              </LineChart>
            ) : (
              <BarChart data={rows} margin={margin} barGap={1}>
                <CartesianGrid {...CHART_GRID_PROPS} />
                {xAxis}
                {yAxis}
                {tooltip}
                {zeroLine}
                {chart.series.map((s, i) => (
                  <Bar key={s.key} dataKey={s.key} name={s.name} fill={chartColor(i)} maxBarSize={28} radius={[3, 3, 0, 0]}>
                    {/* One series: a loss is drawn in the loss colour. Two: colour is the series. */}
                    {chart.series.length === 1 && rows.map((r) => (
                      <Cell key={r.period_end} fill={(r[s.key] ?? 0) < 0 ? LOSS_COLOR : chartColor(i)} />
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
      {Array.from({ length: 8 }, (_, i) => (
        <Card key={i} className="min-w-0">
          <div className="space-y-3 p-[var(--card-padding)]">
            <div className="h-4 w-24 animate-pulse rounded bg-muted" />
            <div className="h-[190px] w-full animate-pulse rounded-md bg-muted" />
          </div>
        </Card>
      ))}
    </>
  )
}

/**
 * One company's fundamentals history from its SEC filings, a chart per figure.
 *
 * The browser only draws: TTM, CAGR, YoY, margins and per-share figures are the server's
 * (`/api/company-financials/{id}`), so the period and range toggles are query parameters,
 * never a transform here. See docs/frontend.md, *The Company tab*.
 */
export function CompanyTab() {
  const keepPrevious = useKeepPreviousData()
  const [picked, setPicked] = useState<number | null>(null)
  const [stored] = useState(readCompanyPick)
  const [period, setPeriod] = useState<CompanyFinancialsPeriod>('ttm')
  const [range, setRange] = useState<CompanyFinancialsRange>('10y')

  const companies = useQuery(companyFinancialsCompaniesQuery)
  const company = companies.data ? resolveCompany(companies.data, picked, stored) : null

  const financials = useQuery({
    ...companyFinancialsQuery(company?.security_id ?? -1, period, range),
    enabled: company != null,
    // The last company's charts stay, dimmed, while the next loads: a skeleton would unmount
    // the toggle just pressed (#68).
    placeholderData: keepPrevious,
  })
  const data = financials.data

  const pick = (securityId: number) => {
    setPicked(securityId)
    writeCompanyPick(securityId)
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
          <p className="py-8 text-center text-sm text-muted-foreground">No companies with SEC filings held.</p>
        </CardContent>
      </Card>
    )
  }

  const charts = data ? visibleCharts(data) : []

  return (
    <div className="space-y-4">
      <Card>
        <SectionHeader
          title={
            <span className="flex min-w-0 items-center gap-2">
              {company && <TickerAvatar symbol={company.symbol} securityId={company.security_id} />}
              {companies.data ? (
                <select
                  aria-label="Company"
                  value={company?.security_id ?? ''}
                  onChange={(e) => pick(Number(e.target.value))}
                  className="h-9 min-w-0 max-w-full rounded-lg border border-border bg-card px-2.5 text-sm font-medium transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {companies.data.map((c) => (
                    <option key={c.security_id} value={c.security_id}>
                      {c.symbol} · {displayName(c.name)}
                    </option>
                  ))}
                </select>
              ) : (
                <span className="h-9 w-48 animate-pulse rounded-lg bg-muted" />
              )}
            </span>
          }
          aside={
            <>
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
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
              {data ? charts.map((chart) => <CompanyChartCard key={chart.id} chart={chart} data={data} />) : <ChartSkeletons />}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
