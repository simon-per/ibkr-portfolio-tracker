/**
 * Analytics — where the return came from, and what the risk looked like over time.
 *
 * Four questions, each its own card, all served from tables that already exist (no request
 * here can reach Yahoo):
 *
 * 1. **Return decomposition** (`/api/performance/decomposition`): the change in Total Value
 *    split into money paid in, price, FX, dividends, the cash adjustment and an explicit
 *    remainder, as a waterfall for the range and clustered bars per calendar year. The legs
 *    sum EXACTLY to end − start; the remainder is a named bar, never folded into another leg.
 * 2. **Segments** (`/api/performance/segments`): the same gains folded onto sectors and
 *    countries through the look-through. Badged as the approximation it is — a fund is spread
 *    by its *current* basket.
 * 3. **Risk over time**: 12-month rolling Sharpe, volatility and beta, plus a drawdown ledger,
 *    computed client-side from the value series (and the Performance tab's first selected
 *    benchmark) with `lib/rollingRisk.ts`. Reuses `portfolioKpis.ts`' primitives, so the
 *    last rolling point equals the risk cards over the same window.
 * 4. **Closed positions** (`/api/performance/closed-positions`): what was realized, how long
 *    it was held, and what the price did after the sale.
 *
 * Conventions, each a bug elsewhere first: an unknown renders `—` (ABSENT), never 0; an
 * absent benchmark leaves beta and the benchmark column blank with the reason beside them;
 * every chart uses the shared theme-aware tooltip style (the default was unreadable in dark
 * mode).
 *
 * **Caveats are a one-line count that expands, at the owner's request (2026-09-13).** The
 * first version put every `warnings[]` sentence in an amber block above the figures; ten
 * lines of it took the top of the tab. The rule this codebase keeps — a caveat inside a
 * collapsible does not exist — is met the way the Look-through tab's fund coverage meets it:
 * the material qualifier (how many holdings were left out) stays on the surface as the
 * collapsed line's own text, and the itemised sentences sit behind it.
 */
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { KpiCard, KpiCardSkeleton, ABSENT } from '@/components/ui/KpiCard'
import { DataTable, type Column } from '@/components/ui/DataTable'
import { api } from '@/lib/api'
import type {
  ClosedPosition,
  ClosedPositionsResponse,
  DecompositionWindow,
  DecompositionYear,
  SegmentAttributionResponse,
  SegmentRow,
} from '@/lib/api'
import { useCurrencySymbol, useFormatCurrency } from '@/lib/CurrencyContext'
import { rangeFor, TIME_RANGES, type TimeRange } from '@/lib/dateRanges'
import { benchmarkAsValueSeries } from '@/lib/portfolioKpis'
import {
  drawdownEpisodes,
  rollingMetrics,
  ROLLING_WINDOW_DAYS,
  type DrawdownEpisode,
} from '@/lib/rollingRisk'
import { LEGS, waterfallRows, type LegKey, type WaterfallRow } from '@/lib/returnDecomposition'
import { allocationSectorPaint, neutralPaint } from '@/lib/sectorColors'
import {
  CHART_TOOLTIP_ITEM_STYLE,
  CHART_TOOLTIP_LABEL_STYLE,
  CHART_TOOLTIP_STYLE,
} from '@/lib/chartTooltip'
import { formatDate } from '@/lib/utils'
import { useIsCompact } from '@/lib/useMediaQuery'

const GAIN = '#16a34a'
const LOSS = '#dc2626'
const TOTAL = '#64748b'
const FLOW = '#94a3b8'
/** Leg colours: price and FX are the two the eye should separate first. */
const LEG_COLORS: Record<LegKey, string> = {
  price_effect_eur: '#2563eb',
  fx_effect_eur: '#f59e0b',
  dividends_eur: '#16a34a',
  cash_adjustment_eur: '#0891b2',
  unsplit_eur: '#a855f7',
  unexplained_eur: '#9ca3af',
}
const GRID = 'hsl(var(--border))'
const AXIS = 'hsl(var(--muted-foreground))'

/** One wrapper height per chart, shared by the chart and its empty/loading states. */
const CHART_CLASS = 'h-64 w-full sm:h-72'
const DRAWDOWN_THRESHOLD_PCT = 5

const tooltipProps = {
  contentStyle: CHART_TOOLTIP_STYLE,
  itemStyle: CHART_TOOLTIP_ITEM_STYLE,
  labelStyle: CHART_TOOLTIP_LABEL_STYLE,
  cursor: { fill: 'hsl(var(--muted))', opacity: 0.4 },
}

export interface AnalyticsBenchmark {
  key: string
  name: string
}

interface AnalyticsTabProps {
  /** The portfolio's real inception, for the ALL range. */
  inception: string | null
  /** The Performance tab's first selected benchmark, or null when none is selected. */
  benchmark: AnalyticsBenchmark | null
}

function pct(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return ABSENT
  return `${v >= 0 ? '+' : ''}${v.toFixed(digits)}%`
}

/** "12.3k" style axis labels with the currency symbol, so a 60px axis is enough. */
function useCompactMoney() {
  const symbol = useCurrencySymbol()
  const fmt = useMemo(
    () => new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }),
    [],
  )
  return (v: number) => `${v < 0 ? '−' : ''}${symbol}${fmt.format(Math.abs(v))}`
}

/**
 * The collapsed caveat line. `surface` is the qualifier that must stay visible when closed;
 * the itemised `items` open beneath it.
 */
function Caveats({ items, surface }: { items: string[] | undefined; surface?: string | null }) {
  if (!items || items.length === 0) return null
  return (
    <details className="group mb-2 text-xs text-muted-foreground">
      <summary className="cursor-pointer select-none list-none marker:content-none">
        <span className="inline-block transition-transform group-open:rotate-90">▸</span>{' '}
        {items.length} note{items.length === 1 ? '' : 's'}
        {surface ? ` · ${surface}` : ''}
      </summary>
      <ul className="mt-1 space-y-1 border-l-2 border-border pl-3">
        {items.map((w) => <li key={w}>{w}</li>)}
      </ul>
    </details>
  )
}

function LoadFailed({ what }: { what: string }) {
  return (
    <p role="alert" className="text-sm text-red-600 dark:text-red-400">
      Could not load {what}. The figures are absent, not zero.
    </p>
  )
}

function unpricedSurface(n: number | undefined): string | null {
  if (!n) return null
  return `${n} unpriced holding${n === 1 ? '' : 's'} left out`
}

export function AnalyticsTab({ inception, benchmark }: AnalyticsTabProps) {
  const formatCurrency = useFormatCurrency()
  const compactMoney = useCompactMoney()
  const isCompact = useIsCompact()
  const [selectedRange, setSelectedRange] = useState<TimeRange>('1Y')
  const dateRange = useMemo(
    () => rangeFor(selectedRange, new Date(), inception),
    [selectedRange, inception],
  )

  const decomposition = useQuery({
    queryKey: ['performance', 'decomposition', dateRange],
    queryFn: () => api.getReturnDecomposition(dateRange.start, dateRange.end),
  })
  const segments = useQuery({
    queryKey: ['performance', 'segments', dateRange],
    queryFn: () => api.getSegmentAttribution(dateRange.start, dateRange.end),
  })
  // Same key as the Performance tab's chart query, so switching tabs costs no request.
  const timeline = useQuery({
    queryKey: ['portfolio', 'value-over-time', dateRange],
    queryFn: () => api.getPortfolioValueOverTime(dateRange.start, dateRange.end),
  })
  // Only when the Performance tab already asks for this benchmark: on a cache miss the
  // benchmark endpoint fetches Yahoo, and this tab must never be the first to ask.
  const benchmarkQuery = useQuery({
    queryKey: ['portfolio', 'benchmark', dateRange, benchmark?.key, 'window'],
    queryFn: () => api.getBenchmarkComparison(dateRange.start, dateRange.end, benchmark!.key),
    enabled: benchmark !== null,
  })
  const closed = useQuery({
    queryKey: ['performance', 'closed-positions'],
    queryFn: () => api.getClosedPositions(),
  })

  const benchSeries = useMemo(
    () => (benchmarkQuery.data ? benchmarkAsValueSeries(benchmarkQuery.data.data) : undefined),
    [benchmarkQuery.data],
  )
  const rolling = useMemo(
    () => (timeline.data ? rollingMetrics(timeline.data, benchSeries) : []),
    [timeline.data, benchSeries],
  )
  const episodes = useMemo(
    () => (timeline.data ? drawdownEpisodes(timeline.data, benchSeries, DRAWDOWN_THRESHOLD_PCT) : []),
    [timeline.data, benchSeries],
  )

  const w = decomposition.data?.window
  const tone = (v: number | null) => (v === null ? 'muted' : v >= 0 ? 'positive' : 'negative')

  return (
    <div className="space-y-6">
      {/* Range */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Where the return came from</h2>
          <p className="text-sm text-muted-foreground">
            {formatDate(dateRange.start)} to {formatDate(dateRange.end)}
            {w && w.start_date !== dateRange.start ? ` · valued from ${formatDate(w.start_date)}` : ''}
          </p>
        </div>
        <div
          className="flex gap-1 overflow-x-auto snap-x [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
          role="group"
          aria-label="Time range"
        >
          {TIME_RANGES.map((range) => (
            <Button
              key={range}
              variant={selectedRange === range ? 'default' : 'outline'}
              size="sm"
              onClick={() => setSelectedRange(range)}
              className="shrink-0 snap-start"
            >
              {range}
            </Button>
          ))}
        </div>
      </div>

      {/* 1. Decomposition KPIs */}
      <section aria-labelledby="decomposition-title">
        <Caveats items={w?.warnings} surface={unpricedSurface(w?.unpriced_holdings)} />
        {decomposition.isError ? (
          <LoadFailed what="the return decomposition" />
        ) : decomposition.isLoading || !w ? (
          <KpiCardSkeleton count={6} />
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:gap-4 md:grid-cols-3 lg:grid-cols-6">
            <KpiCard
              label="Gain"
              hero
              value={w.gain_eur === null ? null : formatCurrency(w.gain_eur)}
              tone={tone(w.gain_eur)}
              sub={w.gain_pct === null
                ? 'Total Value change less money paid in'
                : `${pct(w.gain_pct)} Modified Dietz · ${formatCurrency(w.net_flows_eur ?? 0)} paid in`}
            />
            <KpiCard
              label="Price"
              value={w.price_effect_eur === null ? null : formatCurrency(w.price_effect_eur)}
              tone={tone(w.price_effect_eur)}
              sub="Earned in each holding's own currency"
            />
            <KpiCard
              label="FX"
              value={w.fx_effect_eur === null ? null : formatCurrency(w.fx_effect_eur)}
              tone={tone(w.fx_effect_eur)}
              sub={`${decomposition.data!.base_currency} against the holdings' currencies`}
            />
            <KpiCard
              label="Dividends"
              value={w.dividends_eur === null ? null : formatCurrency(w.dividends_eur)}
              tone={w.dividends_eur === null ? 'muted' : 'positive'}
              sub="Net cash that landed"
            />
            <KpiCard
              label="Cash adjustment"
              value={w.cash_adjustment_eur === null ? null : formatCurrency(w.cash_adjustment_eur)}
              tone={tone(w.cash_adjustment_eur)}
              sub={w.cash_adjustment_eur === null
                ? 'Needs a measured cash balance'
                : w.cash_adjustment_eur >= 0
                  ? "IBKR holds more than the ledgers explain"
                  : "IBKR holds less than the ledgers explain"}
              title="The difference between IBKR's own cash balance and what trades, deposits and dividends add up to: broker interest, account fees, FX on idle cash, and any gap in the ledgers. Positive is not a fee bill — it is cash the account has that the ledgers cannot account for."
            />
            <KpiCard
              label="Unexplained"
              value={w.unexplained_eur === null ? null : formatCurrency(w.unexplained_eur)}
              tone="muted"
              sub="Commissions, transfers, rounding"
              title="The remainder that makes the legs sum exactly to the change in total value: trading commissions, in-kind transfers, and the gap between a sale's real proceeds and the market price its lot carries."
            />
          </div>
        )}
      </section>

      <Card>
        <CardHeader>
          <CardTitle id="decomposition-title">Return decomposition</CardTitle>
          <CardDescription>
            Left: the selected range, from start value to end value. Right: the same legs per
            calendar year, side by side. Money paid in is a flow, not a return, and is drawn grey.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {decomposition.isError ? (
            <LoadFailed what="the return decomposition" />
          ) : (
            <div className="grid gap-8 lg:grid-cols-2 [&>*]:min-w-0">
              <Waterfall window={w} formatCurrency={formatCurrency} compactMoney={compactMoney} compact={isCompact} />
              <YearlyLegs years={decomposition.data?.years} formatCurrency={formatCurrency} compactMoney={compactMoney} compact={isCompact} />
            </div>
          )}
          {decomposition.data && decomposition.data.years.length > 0 && (
            <div className="mt-6">
              <YearTable years={decomposition.data.years} formatCurrency={formatCurrency} />
            </div>
          )}
        </CardContent>
      </Card>

      {/* 2. Segments */}
      <SegmentsCard
        data={segments.data}
        isLoading={segments.isLoading}
        isError={segments.isError}
        formatCurrency={formatCurrency}
        compactMoney={compactMoney}
        compact={isCompact}
      />

      {/* 3. Risk over time */}
      <Card>
        <CardHeader>
          <CardTitle>Risk over time</CardTitle>
          <CardDescription>
            Trailing {ROLLING_WINDOW_DAYS}-trading-day Sharpe, volatility and beta, one point per
            day once the window is full.
            {benchmark
              ? ` Beta is against ${benchmark.name}, on flow-free days only.`
              : ' Pick a benchmark on the Performance tab to see beta.'}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {timeline.isError ? (
            <LoadFailed what="the value series" />
          ) : timeline.isLoading ? (
            <div className={`${CHART_CLASS} animate-pulse rounded-md bg-muted`} />
          ) : rolling.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Needs {ROLLING_WINDOW_DAYS} trading days of history in the selected range to fill one
              window — pick 2Y or ALL.
            </p>
          ) : (
            <div className={CHART_CLASS}>
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={rolling} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke={GRID} vertical={false} />
                  <XAxis dataKey="date" tick={{ fontSize: 11, fill: AXIS }} tickLine={false} axisLine={false} tickFormatter={(d: string) => d.slice(0, 7)} minTickGap={32} />
                  <YAxis yAxisId="ratio" tick={{ fontSize: 11, fill: AXIS }} tickLine={false} axisLine={false} width={36} />
                  <YAxis yAxisId="vol" orientation="right" tick={{ fontSize: 11, fill: AXIS }} tickLine={false} axisLine={false} width={40} tickFormatter={(v: number) => `${v.toFixed(0)}%`} />
                  <Tooltip
                    {...tooltipProps}
                    formatter={(value, name) => {
                      if (value === null || value === undefined) return [ABSENT, String(name)]
                      const n = Number(value)
                      return [name === 'Volatility' ? `${n.toFixed(1)}%` : n.toFixed(2), String(name)]
                    }}
                  />
                  <Legend iconType="plainline" wrapperStyle={{ fontSize: 12 }} />
                  <ReferenceLine yAxisId="ratio" y={0} stroke={AXIS} strokeDasharray="2 2" />
                  <Line yAxisId="ratio" type="monotone" dataKey="sharpe" name="Sharpe" stroke="#2563eb" dot={false} strokeWidth={2} connectNulls={false} />
                  <Line yAxisId="ratio" type="monotone" dataKey="beta" name="Beta" stroke="#a855f7" dot={false} strokeWidth={2} connectNulls={false} />
                  <Line yAxisId="vol" type="monotone" dataKey="volatilityPct" name="Volatility" stroke="#f59e0b" dot={false} strokeWidth={1.5} connectNulls={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Drawdowns */}
      <Card>
        <CardHeader>
          <CardTitle>Drawdowns</CardTitle>
          <CardDescription>
            Every fall deeper than {DRAWDOWN_THRESHOLD_PCT}% from the highest point so far in the
            range, on a flow-adjusted index — a deposit is not a recovery and a withdrawal is not a fall.
            {benchmark ? ` The last column is ${benchmark.name} over the same peak-to-trough dates.` : ''}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {timeline.isError ? (
            <LoadFailed what="the value series" />
          ) : timeline.isLoading ? (
            <p className="text-sm text-muted-foreground">Loading…</p>
          ) : episodes.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No fall deeper than {DRAWDOWN_THRESHOLD_PCT}% in this range.
            </p>
          ) : (
            <DataTable
              rows={episodes}
              columns={drawdownColumns(benchmark?.name ?? null)}
              getRowKey={(e) => e.peakDate}
              label="Drawdowns"
              density="compact"
            />
          )}
        </CardContent>
      </Card>

      {/* 4. Closed positions */}
      <ClosedPositionsCard
        data={closed.data}
        isLoading={closed.isLoading}
        isError={closed.isError}
        formatCurrency={formatCurrency}
        compactMoney={compactMoney}
        compact={isCompact}
      />
    </div>
  )
}

// ---------------------------------------------------------------------------- waterfall

function Waterfall({
  window: w, formatCurrency, compactMoney, compact,
}: {
  window: DecompositionWindow | undefined
  formatCurrency: (v: number) => string
  compactMoney: (v: number) => string
  compact: boolean
}) {
  const rows = useMemo(() => (w ? waterfallRows(w) : []), [w])
  if (!w) return <div className={`${CHART_CLASS} animate-pulse rounded-md bg-muted`} />
  if (rows.length === 0) {
    return <p className="text-sm text-muted-foreground">Nothing could be valued in this range.</p>
  }
  // The visible bars sit well above zero (the start value), so the axis starts near the
  // smallest bar base rather than at 0 — otherwise every leg is a sliver on top of a tower.
  const floor = Math.min(...rows.map((r) => r.base))
  const ceil = Math.max(...rows.map((r) => r.base + r.size))
  const pad = (ceil - floor) * 0.08
  return (
    <div>
      <h3 className="mb-2 text-sm font-medium">Selected range</h3>
      <div className={CHART_CLASS}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={rows} margin={{ top: 20, right: 8, left: 0, bottom: 0 }} barCategoryGap="28%">
            <CartesianGrid strokeDasharray="3 3" stroke={GRID} vertical={false} />
            <XAxis
              dataKey="name"
              tick={{ fontSize: compact ? 10 : 11, fill: AXIS }}
              tickLine={false}
              axisLine={false}
              interval={0}
              angle={compact ? -35 : 0}
              textAnchor={compact ? 'end' : 'middle'}
              height={compact ? 52 : 28}
            />
            <YAxis
              domain={[Math.max(0, floor - pad), ceil + pad]}
              tick={{ fontSize: 11, fill: AXIS }}
              tickLine={false}
              axisLine={false}
              width={52}
              tickFormatter={compactMoney}
            />
            <Tooltip
              {...tooltipProps}
              formatter={(_v, _n, item) => {
                const row = item?.payload as WaterfallRow | undefined
                return row ? [formatCurrency(row.value), row.kind === 'total' ? 'Total Value' : row.name] : [ABSENT, '']
              }}
              labelFormatter={() => ''}
            />
            <Bar dataKey="base" stackId="w" fill="transparent" isAnimationActive={false} />
            <Bar dataKey="size" stackId="w" radius={[3, 3, 0, 0]}>
              {rows.map((r) => (
                <Cell
                  key={r.name}
                  fill={r.kind === 'total' ? TOTAL : r.kind === 'flow' ? FLOW : r.kind === 'gain' ? GAIN : LOSS}
                />
              ))}
              <LabelList
                dataKey="value"
                position="top"
                style={{ fontSize: compact ? 9 : 10, fill: AXIS }}
                formatter={(v: unknown) => compactMoney(Number(v))}
              />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}

// -------------------------------------------------------------------------- per year

function YearlyLegs({
  years, formatCurrency, compactMoney, compact,
}: {
  years: DecompositionYear[] | undefined
  formatCurrency: (v: number) => string
  compactMoney: (v: number) => string
  compact: boolean
}) {
  if (!years) return <div className={`${CHART_CLASS} animate-pulse rounded-md bg-muted`} />
  const rows = years
    .filter((y) => y.gain_eur !== null)
    .map((y) => ({ ...y, label: y.partial ? `${y.year}*` : String(y.year) }))
  if (rows.length === 0) {
    return <p className="text-sm text-muted-foreground">No calendar year could be valued.</p>
  }
  // A leg that is zero or unknown in every year is not drawn: an empty cluster slot for
  // "Unsplit" on an account where every holding split would only add legend noise.
  const legs = LEGS.filter((leg) => rows.some((y) => (y[leg.key] ?? 0) !== 0))
  return (
    <div>
      <h3 className="mb-2 text-sm font-medium">Per calendar year (* partial)</h3>
      <div className={CHART_CLASS}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={rows} margin={{ top: 8, right: 8, left: 0, bottom: 0 }} barCategoryGap="22%" barGap={2}>
            <CartesianGrid strokeDasharray="3 3" stroke={GRID} vertical={false} />
            <XAxis dataKey="label" tick={{ fontSize: 11, fill: AXIS }} tickLine={false} axisLine={false} />
            <YAxis tick={{ fontSize: 11, fill: AXIS }} tickLine={false} axisLine={false} width={52} tickFormatter={compactMoney} />
            <Tooltip {...tooltipProps} formatter={(v, n) => [v === null ? ABSENT : formatCurrency(Number(v)), String(n)]} />
            <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: compact ? 11 : 12 }} />
            <ReferenceLine y={0} stroke={AXIS} strokeDasharray="2 2" />
            {legs.map((leg) => (
              <Bar key={leg.key} dataKey={leg.key} name={leg.label} fill={LEG_COLORS[leg.key]} radius={[3, 3, 0, 0]} />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}

function YearTable({ years, formatCurrency }: { years: DecompositionYear[]; formatCurrency: (v: number) => string }) {
  const money = (v: number | null) => (v === null ? ABSENT : formatCurrency(v))
  const signed = (v: number | null) => (v === null ? undefined : v > 0 ? 'text-green-600 dark:text-green-400' : v < 0 ? 'text-red-600 dark:text-red-400' : undefined)
  const columns: Column<DecompositionYear>[] = [
    { key: 'year', header: 'Year', shortHeader: 'Year', mobile: 'title', cell: (y) => `${y.year}${y.partial ? ' (partial)' : ''}` },
    { key: 'start', header: 'Start value', shortHeader: 'Start', align: 'right', cell: (y) => money(y.start_total_value_eur) },
    { key: 'flows', header: 'Paid in', shortHeader: 'Paid in', align: 'right', cell: (y) => money(y.net_flows_eur), hint: { description: 'Money added to the account in the year; a flow, not a return.' } },
    { key: 'gain', header: 'Gain', shortHeader: 'Gain', align: 'right', mobile: 'value', tone: (y) => signed(y.gain_eur), cell: (y) => money(y.gain_eur) },
    { key: 'gain_pct', header: 'Gain %', shortHeader: 'Gain %', align: 'right', mobile: 'delta', tone: (y) => signed(y.gain_pct), cell: (y) => pct(y.gain_pct), hint: { description: 'Modified Dietz: the gain over the start value plus half the money paid in.' } },
    { key: 'price', header: 'Price', shortHeader: 'Price', align: 'right', tone: (y) => signed(y.price_effect_eur), cell: (y) => money(y.price_effect_eur) },
    { key: 'fx', header: 'FX', shortHeader: 'FX', align: 'right', tone: (y) => signed(y.fx_effect_eur), cell: (y) => money(y.fx_effect_eur) },
    { key: 'div', header: 'Dividends', shortHeader: 'Div', align: 'right', cell: (y) => money(y.dividends_eur) },
    { key: 'cash', header: 'Cash adjustment', shortHeader: 'Cash adj.', align: 'right', tone: (y) => signed(y.cash_adjustment_eur), cell: (y) => money(y.cash_adjustment_eur), hint: { description: "IBKR's measured cash against what the ledgers add up to: interest, fees, FX on idle cash, ledger gaps. Positive means more cash than the ledgers explain." } },
    { key: 'unexplained', header: 'Unexplained', shortHeader: 'Unexpl.', align: 'right', cellClassName: 'text-muted-foreground', cell: (y) => money(y.unexplained_eur), hint: { description: 'The remainder that makes the legs sum exactly to end − start: commissions, in-kind transfers, rounding.' } },
    { key: 'end', header: 'End value', shortHeader: 'End', align: 'right', cell: (y) => money(y.end_total_value_eur) },
  ]
  return (
    <DataTable
      rows={years}
      columns={columns}
      getRowKey={(y) => y.year}
      label="Return decomposition per year"
      density="compact"
      minWidthClassName="min-w-[960px]"
    />
  )
}

// --------------------------------------------------------------------------- segments

type Dimension = 'sector' | 'country'

function SegmentsCard({
  data, isLoading, isError, formatCurrency, compactMoney, compact,
}: {
  data: SegmentAttributionResponse | undefined
  isLoading: boolean
  isError: boolean
  formatCurrency: (v: number) => string
  compactMoney: (v: number) => string
  compact: boolean
}) {
  const [dimension, setDimension] = useState<Dimension>('sector')
  const rows: SegmentRow[] = useMemo(() => {
    if (!data) return []
    const list = dimension === 'sector' ? data.by_sector : data.by_country
    return [...list].sort((a, b) => (b.pnl_eur ?? 0) - (a.pnl_eur ?? 0))
  }, [data, dimension])
  const paint = (name: string) => (dimension === 'sector' ? allocationSectorPaint(name) : neutralPaint(name)).fill
  const chartHeight = Math.min(compact ? 480 : 640, Math.max(200, rows.length * (compact ? 26 : 30)))

  const money = (v: number | null) => (v === null ? ABSENT : formatCurrency(v))
  const columns: Column<SegmentRow>[] = [
    { key: 'name', header: dimension === 'sector' ? 'Sector' : 'Country', shortHeader: 'Segment', mobile: 'title', cellClassName: 'font-medium', cell: (r) => r.name },
    { key: 'pnl', header: 'Gain', shortHeader: 'Gain', align: 'right', mobile: 'value', tone: (r) => (r.pnl_eur ?? 0) > 0 ? 'text-green-600 dark:text-green-400' : (r.pnl_eur ?? 0) < 0 ? 'text-red-600 dark:text-red-400' : undefined, cell: (r) => money(r.pnl_eur) },
    { key: 'share', header: 'Share of gain', shortHeader: 'Share', align: 'right', mobile: 'delta', cell: (r) => pct(r.share_of_gain_pct), hint: { description: 'This segment’s gain as a share of the whole gain. Segments that lost money read negative; shares can exceed 100% when others lost.' } },
    { key: 'price', header: 'Price', shortHeader: 'Price', align: 'right', cell: (r) => money(r.price_effect_eur) },
    { key: 'fx', header: 'FX', shortHeader: 'FX', align: 'right', cell: (r) => money(r.fx_effect_eur) },
    { key: 'start_w', header: 'Weight at start', shortHeader: 'Start %', align: 'right', cell: (r) => r.start_weight_pct === null ? ABSENT : `${r.start_weight_pct.toFixed(1)}%`, hint: { description: 'Share of the priced book at the start of the range. Compare with the share of gain: a segment earning more than its weight pulled its weight.' } },
    { key: 'end_w', header: 'Weight at end', shortHeader: 'End %', align: 'right', cell: (r) => r.end_weight_pct === null ? ABSENT : `${r.end_weight_pct.toFixed(1)}%` },
    { key: 'via', header: 'Via funds', shortHeader: 'Via funds', align: 'right', mobile: 'badge', cell: (r) => r.via_funds_pct === null ? ABSENT : `${r.via_funds_pct.toFixed(0)}%`, hint: { description: 'How much of the segment’s end value arrived through fund baskets rather than direct holdings.' } },
  ]

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle>Which parts of the book made it</CardTitle>
            <CardDescription>
              Each holding's gain over the range, folded onto {dimension === 'sector' ? 'sectors' : 'countries'} —
              funds through their baskets, company by company. Nothing is rescaled: what a
              basket cannot place stays visible as its own row.
            </CardDescription>
          </div>
          <div className="flex gap-1" role="group" aria-label="Segment dimension">
            {(['sector', 'country'] as Dimension[]).map((d) => (
              <Button key={d} size="sm" variant={dimension === d ? 'default' : 'outline'} onClick={() => setDimension(d)}>
                {d === 'sector' ? 'Sector' : 'Country'}
              </Button>
            ))}
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <Caveats items={data?.warnings} surface={unpricedSurface(data?.unpriced_holdings)} />
        {isError ? (
          <LoadFailed what="the segment attribution" />
        ) : isLoading || !data ? (
          <div className={`${CHART_CLASS} animate-pulse rounded-md bg-muted`} />
        ) : rows.length === 0 ? (
          <p className="text-sm text-muted-foreground">Nothing could be valued in this range.</p>
        ) : (
          <>
            <p className="mb-3 text-sm">
              Total gain of the priced book:{' '}
              <span className={`font-semibold ${(data.total_pnl_eur ?? 0) >= 0 ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}`}>
                {money(data.total_pnl_eur)}
              </span>
            </p>
            <div style={{ height: chartHeight }} className="w-full">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 16, left: 8, bottom: 4 }} barCategoryGap="25%">
                  <CartesianGrid strokeDasharray="3 3" stroke={GRID} horizontal={false} />
                  <XAxis type="number" tick={{ fontSize: 11, fill: AXIS }} tickLine={false} axisLine={false} tickFormatter={compactMoney} />
                  <YAxis type="category" dataKey="name" width={compact ? 110 : 170} tick={{ fontSize: compact ? 10 : 11, fill: AXIS }} tickLine={false} axisLine={false} interval={0} />
                  <Tooltip {...tooltipProps} formatter={(v) => [v === null ? ABSENT : formatCurrency(Number(v)), 'Gain']} />
                  <ReferenceLine x={0} stroke={AXIS} />
                  <Bar dataKey="pnl_eur" name="Gain" radius={[0, 3, 3, 0]}>
                    {rows.map((r) => (
                      <Cell key={r.name} fill={paint(r.name)} fillOpacity={(r.pnl_eur ?? 0) < 0 ? 0.55 : 1} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
            <div className="mt-4">
              <DataTable
                rows={rows}
                columns={columns}
                getRowKey={(r) => r.name}
                label={`Gain by ${dimension}`}
                density="compact"
                minWidthClassName="min-w-[840px]"
              />
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}

// --------------------------------------------------------------------------- drawdowns

function drawdownColumns(benchmarkName: string | null): Column<DrawdownEpisode>[] {
  return [
    { key: 'peak', header: 'Peak', shortHeader: 'Peak', mobile: 'title', cell: (e) => formatDate(e.peakDate) },
    { key: 'trough', header: 'Trough', shortHeader: 'Trough', mobile: 'meta', cell: (e) => formatDate(e.troughDate) },
    { key: 'depth', header: 'Depth', shortHeader: 'Depth', align: 'right', mobile: 'value', tone: () => 'text-red-600 dark:text-red-400', cell: (e) => `${e.depthPct.toFixed(2)}%` },
    { key: 'to_trough', header: 'Days to trough', shortHeader: 'To trough', align: 'right', cell: (e) => String(e.daysToTrough) },
    { key: 'recovered', header: 'Recovered', shortHeader: 'Recovered', mobile: 'badge', cell: (e) => e.recoveredDate ? formatDate(e.recoveredDate) : 'still open' },
    { key: 'to_recover', header: 'Days to recover', shortHeader: 'To recover', align: 'right', cell: (e) => e.daysToRecover === null ? ABSENT : String(e.daysToRecover) },
    {
      key: 'bench',
      header: benchmarkName ? `${benchmarkName} same dates` : 'Benchmark same dates',
      shortHeader: 'Benchmark',
      align: 'right',
      mobile: 'delta',
      cell: (e) => pct(e.benchmarkPct),
      hint: { description: benchmarkName ? `${benchmarkName}'s own move from the peak date to the trough date.` : 'Pick a benchmark on the Performance tab to fill this column.' },
    },
  ]
}

// ---------------------------------------------------------------------- closed positions

function ClosedPositionsCard({
  data, isLoading, isError, formatCurrency, compactMoney, compact,
}: {
  data: ClosedPositionsResponse | undefined
  isLoading: boolean
  isError: boolean
  formatCurrency: (v: number) => string
  compactMoney: (v: number) => string
  compact: boolean
}) {
  const money = (v: number | null) => (v === null ? ABSENT : formatCurrency(v))
  const signed = (v: number | null) => (v === null ? undefined : v > 0 ? 'text-green-600 dark:text-green-400' : v < 0 ? 'text-red-600 dark:text-red-400' : undefined)
  const columns: Column<ClosedPosition>[] = [
    { key: 'symbol', header: 'Security', shortHeader: 'Security', mobile: 'title', cellClassName: 'font-medium', cell: (p) => `${p.symbol}${p.still_held ? ' (partly)' : ''}` },
    { key: 'dates', header: 'Held', shortHeader: 'Held', mobile: 'meta', cellClassName: 'text-muted-foreground text-xs', cell: (p) => `${formatDate(p.first_open_date)} → ${formatDate(p.last_close_date)}` },
    { key: 'days', header: 'Days held', shortHeader: 'Days', align: 'right', cell: (p) => p.holding_days === null ? ABSENT : String(p.holding_days), hint: { description: 'Cost-weighted across the closed lots.' } },
    { key: 'cost', header: 'Cost', shortHeader: 'Cost', align: 'right', cell: (p) => money(p.cost_basis_eur) },
    { key: 'realized', header: 'Realized', shortHeader: 'Realized', align: 'right', mobile: 'value', tone: (p) => signed(p.realized_pnl_eur), cell: (p) => money(p.realized_pnl_eur), hint: { description: "IBKR's own FIFO figure where a SELL trade is on record; otherwise the market-price approximation the tax report also uses." } },
    { key: 'ret', header: 'Return', shortHeader: 'Return', align: 'right', mobile: 'delta', tone: (p) => signed(p.return_pct), cell: (p) => pct(p.return_pct) },
    { key: 'source', header: 'Source', shortHeader: 'Source', mobile: 'badge', cellClassName: 'text-xs text-muted-foreground', cell: (p) => p.realized_source === 'trade' ? 'IBKR' : 'lots' },
    { key: 'after', header: 'Since sale', shortHeader: 'Since sale', align: 'right', tone: (p) => signed(p.post_sale_pct), cell: (p) => p.post_sale_pct === null ? ABSENT : `${pct(p.post_sale_pct)} · ${p.post_sale_days}d`, hint: { description: 'The quote’s move from the last close before the sale to the newest close on record. Blank when no price after the sale exists.' } },
  ]
  const s = data?.summary
  const chartRows = (data?.positions ?? []).filter((p) => p.realized_pnl_eur !== null)
  const chartHeight = Math.min(compact ? 420 : 560, Math.max(160, chartRows.length * (compact ? 26 : 30)))

  return (
    <Card>
      <CardHeader>
        <CardTitle>Closed positions</CardTitle>
        <CardDescription>
          What the sold positions realized, how long they were held, and what their prices did
          after the sale — the one figure that judges the decision rather than the holding.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Caveats items={data?.warnings} />
        {isError ? (
          <LoadFailed what="the closed positions" />
        ) : isLoading || !data || !s ? (
          <KpiCardSkeleton count={4} />
        ) : data.positions.length === 0 ? (
          <p className="text-sm text-muted-foreground">Nothing has been sold yet.</p>
        ) : (
          <>
            <div className="mb-4 grid grid-cols-2 gap-3 sm:gap-4 md:grid-cols-4">
              <KpiCard
                label="Realized"
                value={s.total_realized_eur === null ? null : formatCurrency(s.total_realized_eur)}
                tone={s.total_realized_eur === null ? 'muted' : s.total_realized_eur >= 0 ? 'positive' : 'negative'}
                sub={`${s.closed_securities} securities sold${s.total_cost_eur !== null ? ` · ${formatCurrency(s.total_cost_eur)} cost` : ''}`}
              />
              <KpiCard
                label="Hit rate"
                value={s.hit_rate_pct === null ? null : `${s.hit_rate_pct.toFixed(0)}%`}
                tone={s.hit_rate_pct === null ? 'muted' : s.hit_rate_pct >= 50 ? 'positive' : 'warning'}
                sub={`${s.winners} winners · ${s.losers} losers`}
              />
              <KpiCard
                label="Avg holding"
                value={s.avg_holding_days === null ? null : `${s.avg_holding_days} d`}
                sub="Cost-weighted, open to close"
              />
              <KpiCard
                label="Rose after sale"
                value={s.post_sale_judged === 0 ? null : `${s.sold_then_rose} of ${s.post_sale_judged}`}
                tone={s.post_sale_judged === 0 ? 'muted' : s.sold_then_rose > s.sold_then_fell ? 'warning' : 'positive'}
                sub={s.post_sale_judged === 0 ? 'No price after any sale on record' : 'Positions that kept rising once sold'}
              />
            </div>
            {chartRows.length > 0 && (
              <div style={{ height: chartHeight }} className="w-full">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={chartRows} layout="vertical" margin={{ top: 4, right: 16, left: 8, bottom: 4 }} barCategoryGap="25%">
                    <CartesianGrid strokeDasharray="3 3" stroke={GRID} horizontal={false} />
                    <XAxis type="number" tick={{ fontSize: 11, fill: AXIS }} tickLine={false} axisLine={false} tickFormatter={compactMoney} />
                    <YAxis type="category" dataKey="symbol" width={compact ? 70 : 90} tick={{ fontSize: 11, fill: AXIS }} tickLine={false} axisLine={false} interval={0} />
                    <Tooltip {...tooltipProps} formatter={(v) => [v === null ? ABSENT : formatCurrency(Number(v)), 'Realized']} />
                    <ReferenceLine x={0} stroke={AXIS} />
                    <Bar dataKey="realized_pnl_eur" name="Realized" radius={[0, 3, 3, 0]}>
                      {chartRows.map((p) => (
                        <Cell key={p.security_id} fill={(p.realized_pnl_eur ?? 0) >= 0 ? GAIN : LOSS} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
            <div className="mt-4">
              <DataTable
                rows={data.positions}
                columns={columns}
                getRowKey={(p) => p.security_id}
                label="Closed positions"
                density="compact"
                minWidthClassName="min-w-[880px]"
              />
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}
