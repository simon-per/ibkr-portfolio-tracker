import { useState, useMemo, useEffect, useRef } from 'react'
import {
  ComposedChart,
  Area,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from 'recharts'
import type { PortfolioValuePoint, BenchmarkValuePoint } from '@/lib/api'
import { formatDate, tooltipValue, formatAxisMoney } from '@/lib/utils'
import { dateAxis } from '@/lib/dateAxis'
import { useFormatCurrency, useCurrencySymbol } from '@/lib/CurrencyContext'
import { useIsCompact } from '@/lib/useMediaQuery'
import { axisFloor, niceTicks } from '@/lib/niceTicks'
import { cashIsTracked } from '@/lib/portfolioCash'
import { rangeProfitAnchorUnpriced, rangeProfitSeries } from '@/lib/rangeProfit'
import { CHART_TOOLTIP_STYLE, CHART_GRID_PROPS } from '@/lib/chartTooltip'
import { PORTFOLIO_SERIES_COLORS } from '@/lib/benchmarkColors'

/**
 * One height for the chart and its three placeholder states, so a range switch or a
 * fetch failure cannot make the page jump.
 */
const CHART_BOX = 'h-[260px] sm:h-[340px] lg:h-[400px]'

/** The value series' blue, which the area's gradient is drawn from. */
const VALUE_COLOR = PORTFOLIO_SERIES_COLORS.value

/**
 * How far below zero the Y axis may extend, in the **base currency** (EUR/CHF/USD — the
 * three are close enough that one figure covers all, and this is a display bound rather
 * than an amount anyone reconciles).
 *
 * A bound is needed because the axis minimum is rounded *out* to a step multiple, which
 * is unbounded downwards: see the `floor` derivation in `yAxisConfig`, where a portfolio
 * that has never been underwater was still reserving 20k of empty space below zero.
 *
 * 5k is chosen against this account's actual loss history — the deepest drawdown in the
 * series is nowhere near it — and it is a *soft* cap: a real value below it always wins,
 * because clipping a loss off the chart is a worse failure than empty space.
 */
const NEGATIVE_AXIS_FLOOR = -5000

export interface BenchmarkDataset {
  data: BenchmarkValuePoint[]
  name: string
  color: string
  key: string
  /** Holdings the anchor value could not price. Above 0 the whole benchmark line is understated. */
  anchorUnpriced?: number
}

interface PortfolioValueChartProps {
  data: PortfolioValuePoint[]
  benchmarks?: BenchmarkDataset[]
  isLoading?: boolean
  isError?: boolean
}

export function PortfolioValueChart({ data, benchmarks = [], isLoading, isError }: PortfolioValueChartProps) {
  const formatCurrency = useFormatCurrency()
  const curSym = useCurrencySymbol()
  // Named for their ROLE, not their contents, because what each draws depends on
  // whether the backend tracks cash: the baseline is Money In when it does and Cost
  // Basis when it does not, and the value line is the whole account or just its
  // holdings. See `cashTracked`.
  const [showBaseline, setShowBaseline] = useState(true)
  const [showValue, setShowValue] = useState(true)
  const [showProfit, setShowProfit] = useState(true)
  const [showCash, setShowCash] = useState(false)
  const [visibleBenchmarks, setVisibleBenchmarks] = useState<Set<string>>(new Set())
  const seenBenchmarkKeys = useRef<Set<string>>(new Set())
  // Axis width, tick density and margins are Recharts props, not CSS — the one part of
  // going responsive that a media query cannot reach.
  const isCompact = useIsCompact()

  // Days the backend could not fully value. Their market value omits the unpriced
  // holdings while their cost basis keeps them, so the line dips for a reason that is
  // not a loss — a stalled market-data sync reads as a smooth decay toward zero. The
  // risk metrics already exclude these pairs (`isMeasurable`); the line still plots
  // them, so it has to say so rather than let the shape speak for itself.
  const incomplete = useMemo(() => {
    const days = (data ?? []).filter((p) => (p.unpriced_holdings ?? 0) > 0)
    return {
      days: days.length,
      worst: days.reduce((m, p) => Math.max(m, p.unpriced_holdings ?? 0), 0),
      from: days.length ? days[0].date : null,
    }
  }, [data])

  /** Whether to draw the cash-inclusive pair; see `cashIsTracked` for the refusals. */
  const cashTracked = useMemo(() => cashIsTracked(data?.[data.length - 1]), [data])

  /**
   * The lines, and what each one means in this mode.
   *
   * With cash tracked the pair becomes **Total Value vs Money In**, which is the only
   * pairing a rotation cannot put a step in: selling moves value from holdings to cash,
   * and `total_value_eur` already contains both, while `money_in_eur` counts
   * contributions and a sale is not one. The old pair stepped twice on 2026-08-21 —
   * market value fell 24,711 and cost basis fell 18,801 — for a day on which the
   * account lost nothing.
   *
   * Profit is then the gap between them, which is TOTAL profit (realized, unrealized
   * and dividends) rather than the unrealized-only figure the holdings pair produces.
   * Both are honest; they are different quantities, so the label changes with the mode.
   * The line draws that gap's change since the range's first point, not the gap itself.
   */
  const series = useMemo(() => {
    const baseline = cashTracked
      ? { key: 'money_in_eur', name: 'Money In' }
      : { key: 'cost_basis_eur', name: 'Cost Basis' }
    const value = cashTracked
      ? { key: 'total_value_eur', name: 'Total Value' }
      : { key: 'market_value_eur', name: 'Market Value' }
    return [
      // Total Value carries the primary blue; Money In is the quiet reference line it is
      // read against; cash and profit take the two secondary hues. Hex rather than CSS
      // variables because the legend chips derive an alpha background from the string.
      { ...baseline, color: PORTFOLIO_SERIES_COLORS.moneyIn, show: showBaseline, toggle: () => setShowBaseline(v => !v) },
      { ...value, color: VALUE_COLOR, show: showValue, toggle: () => setShowValue(v => !v) },
      ...(cashTracked
        ? [{ key: 'cash_eur', name: 'Cash', color: PORTFOLIO_SERIES_COLORS.cash, show: showCash, toggle: () => setShowCash(v => !v) }]
        : []),
      { key: 'profit_eur', name: 'Profit/Loss', color: PORTFOLIO_SERIES_COLORS.profit, show: showProfit, toggle: () => setShowProfit(v => !v) },
    ]
  }, [cashTracked, showBaseline, showValue, showCash, showProfit])

  // Auto-enable benchmarks when they first appear in the data
  useEffect(() => {
    const newKeys = benchmarks
      .filter(b => b.data.length > 0 && !seenBenchmarkKeys.current.has(b.key))
      .map(b => b.key)

    if (newKeys.length > 0) {
      newKeys.forEach(k => seenBenchmarkKeys.current.add(k))
      setVisibleBenchmarks(prev => {
        const next = new Set(prev)
        newKeys.forEach(k => next.add(k))
        return next
      })
    }
  }, [benchmarks])

  const toggleBenchmark = (key: string) => {
    setVisibleBenchmarks(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const chartData = useMemo(() => {
    // Build lookups for each benchmark by date
    const benchmarkLookups: Record<string, Record<string, number>> = {}
    for (const b of benchmarks) {
      const lookup: Record<string, number> = {}
      for (const bp of b.data) {
        lookup[bp.date] = bp.benchmark_value_eur
      }
      benchmarkLookups[b.key] = lookup
    }

    // Profit follows whichever pair is on screen, rebased to 0 on the range's first point
    // so it starts where the benchmarks do. See `rangeProfit.ts`.
    const profits = rangeProfitSeries(data, cashTracked)

    return data.map((point, i) => {
      const profit = profits[i]
      const row: Record<string, number | string | null> = {
        cost_basis_eur: point.cost_basis_eur,
        market_value_eur: point.market_value_eur,
        total_value_eur: point.total_value_eur ?? null,
        money_in_eur: point.money_in_eur ?? null,
        cash_eur: point.cash_eur ?? null,
        profit_eur: profit,
        date: point.date,
        dateFormatted: formatDate(point.date),
      }
      for (const b of benchmarks) {
        row[`bench_${b.key}`] = benchmarkLookups[b.key]?.[point.date] ?? null
      }
      return row
    })
  }, [data, benchmarks, cashTracked])

  const availableBenchmarks = benchmarks.filter(b => b.data.length > 0)

  const drawnBenchmarks = availableBenchmarks.filter(b => visibleBenchmarks.has(b.key))
  // A seed the backend could not fully price understates every point after it. Folded into
  // the incomplete-valuation notice rather than left to the line's shape. Reduced to a
  // STRING and a NUMBER here, ahead of the axis memo below, so the JSX touches nothing
  // derived from `availableBenchmarks`: the React Compiler treats a method call on an array
  // aliased from that memo's dependency — even `.join()` on a mapped copy — as a possible
  // later mutation, and refuses to preserve the memo
  // (`react-hooks/preserve-manual-memoization`). Bisected, not guessed.
  const understatedNames = drawnBenchmarks
    .filter(b => (b.anchorUnpriced ?? 0) > 0)
    .map(b => b.name)
  const understatedCount = understatedNames.length
  const understatedText = understatedNames.join(' and ')

  // The profit line is rebased to 0 on the first point. An unpriced first day shifts every
  // later point UP (the anchor's value is short), so it joins the notice with its own
  // direction. Only while the line is drawn.
  const profitAnchorUnpriced = showProfit ? rangeProfitAnchorUnpriced(data ?? []) > 0 : false

  // One tick per month (or per day on a short span) — see `dateAxis`.
  const xAxis = useMemo(() => dateAxis(chartData.map((p) => String(p.date))), [chartData])

  // Calculate dynamic Y axis domain and ticks based on data and visible lines
  const yAxisConfig = useMemo(() => {
    if (!chartData || chartData.length === 0) {
      return {
        domain: [0, 50000] as [number, number],
        ticks: [0, 10000, 20000, 30000, 40000, 50000],
        step: 10000,
      }
    }


    // Find the min and max values only from visible lines
    const allValues: number[] = []
    chartData.forEach(point => {
      for (const s of series) {
        if (!s.show) continue
        const v = point[s.key] as number | null
        if (v != null) allValues.push(v)
      }
      for (const b of availableBenchmarks) {
        if (visibleBenchmarks.has(b.key)) {
          const v = point[`bench_${b.key}`] as number | null
          if (v != null) allValues.push(v)
        }
      }
    })

    if (allValues.length === 0) {
      return {
        domain: [0, 50000] as [number, number],
        ticks: [0, 10000, 20000, 30000, 40000, 50000],
        step: 10000,
      }
    }

    const minValue = Math.min(...allValues)
    const maxValue = Math.max(...allValues)
    const range = maxValue - minValue

    // For very small ranges, zoom in more. Otherwise use 10% padding
    const absMax = Math.max(Math.abs(maxValue), Math.abs(minValue), 1)
    const paddingPercent = range / absMax < 0.05 ? 0.15 : 0.10

    // Calculate domain with padding (allow negative for profit line)
    const domainMin = minValue - range * paddingPercent
    const domainMax = maxValue + range * paddingPercent

    // The padding above is a share of the WHOLE range, which the market-value line
    // dominates — so the profit line's minimum gets padded by tens of thousands and the
    // axis rounds out to a big negative step. `axisFloor` bounds that; see it for the
    // two rules and for the production numbers that motivated them.
    const floor = axisFloor(minValue, domainMin, NEGATIVE_AXIS_FLOOR)

    // The tick count is a property of how tall the chart is, not of the data. This
    // used to be a fixed 200/1000/2500/10000 step ladder chosen from the range alone,
    // which is how eight labels ended up 35px apart in a 280px-tall phone chart.
    return niceTicks(domainMin, domainMax, isCompact ? 4 : 8, floor)
  }, [chartData, series, visibleBenchmarks, availableBenchmarks, isCompact])

  const formatYAxisTick = (value: number) => formatAxisMoney(value, curSym, yAxisConfig.step)

  if (isLoading) {
    return (
      <div className={`w-full ${CHART_BOX} flex items-center justify-center bg-muted/10 rounded-lg`}>
        <div className="text-muted-foreground">Loading chart data...</div>
      </div>
    )
  }

  // A server error must not impersonate an empty portfolio — the sync CTA
  // below can't fix a backend that isn't answering.
  if (isError) {
    return (
      <div className={`w-full ${CHART_BOX} flex items-center justify-center bg-muted/10 rounded-lg border border-dashed`}>
        <div className="text-center">
          <p className="text-muted-foreground">Couldn't load the chart</p>
          <p className="text-sm text-muted-foreground mt-2">
            The server didn't respond — it may be redeploying. It retries automatically.
          </p>
        </div>
      </div>
    )
  }

  if (!data || data.length === 0) {
    return (
      <div className={`w-full ${CHART_BOX} flex items-center justify-center bg-muted/10 rounded-lg border border-dashed`}>
        <div className="text-center">
          <p className="text-muted-foreground">No portfolio data available</p>
          <p className="text-sm text-muted-foreground mt-2">
            Sync your IBKR data to see your portfolio value over time
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {(incomplete.days > 0 || understatedCount > 0 || profitAnchorUnpriced) && (
        <div
          role="alert"
          className="mx-2 rounded-lg border border-yellow-600/40 sm:mx-0 bg-yellow-600/10 px-3 py-2 text-xs text-yellow-700 dark:text-yellow-500"
        >
          {incomplete.days > 0 && (
            <>
              <span className="font-medium">
                {incomplete.days} {incomplete.days === 1 ? 'day' : 'days'} in this range could not
                be fully valued
              </span>{' '}
              — up to {incomplete.worst} {incomplete.worst === 1 ? 'holding' : 'holdings'} had no
              usable price{incomplete.from ? `, from ${incomplete.from}` : ''}, so the dip is
              missing data rather than a loss.
            </>
          )}
          {understatedCount > 0 && (
            <>
              {incomplete.days > 0 ? ' ' : ''}
              <span className="font-medium">
                The {understatedText} benchmark{' '}
                {understatedCount === 1 ? 'line starts' : 'lines start'} from a day the
                portfolio could not be fully valued
              </span>
              , so {understatedCount === 1 ? 'it is' : 'they are'} understated for the whole range.
            </>
          )}
          {profitAnchorUnpriced && (
            <>
              {incomplete.days > 0 || understatedCount > 0 ? ' ' : ''}
              <span className="font-medium">
                The Profit/Loss line starts from a day the portfolio could not be fully valued
              </span>
              , so it is overstated for the whole range.
            </>
          )}
        </div>
      )}

      {/* Series toggles. The min-height is the phone's touch target. */}
      <div className="flex flex-wrap items-center gap-1 px-2 sm:gap-1.5 sm:px-0">
        {series.map(s => (
          <button
            key={s.key}
            onClick={s.toggle}
            aria-pressed={s.show}
            className={`flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium transition-colors ${
              s.show ? 'bg-muted text-foreground' : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            <span
              className="h-2 w-2 rounded-full"
              style={{ backgroundColor: s.show ? s.color : 'hsl(var(--muted-foreground) / 0.3)' }}
            />
            {s.name}
          </button>
        ))}
        {availableBenchmarks.map(b => (
          <button
            key={b.key}
            onClick={() => toggleBenchmark(b.key)}
            aria-pressed={visibleBenchmarks.has(b.key)}
            className={`flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium transition-colors ${
              visibleBenchmarks.has(b.key) ? 'bg-muted text-foreground' : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            <span className="h-2 w-2 rounded-full" style={{ backgroundColor: visibleBenchmarks.has(b.key) ? b.color : 'hsl(var(--muted-foreground) / 0.3)' }} />
            {b.name}
          </button>
        ))}
      </div>

      {/* Chart. The height is CSS on the wrapper rather than a `height={600}` prop, so
          one class string covers the chart and its three placeholder states. */}
      <div className={`w-full ${CHART_BOX}`}>
      <ResponsiveContainer width="100%" height="100%">
        {/* `left: 20` double-padded a chart that already has a Y axis — 20px of a
            324px card. */}
        <ComposedChart data={chartData} margin={{ top: 8, right: isCompact ? 8 : 16, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="portfolio-value-fill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={VALUE_COLOR} stopOpacity={0.22} />
              <stop offset="100%" stopColor={VALUE_COLOR} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid {...CHART_GRID_PROPS} />
          <XAxis
            dataKey="date"
            className="text-xs"
            axisLine={false}
            tickLine={false}
            tickMargin={8}
            tick={{ fill: 'hsl(var(--muted-foreground))' }}
            ticks={xAxis.ticks}
            tickFormatter={xAxis.tickFormatter}
            interval="preserveStartEnd"
            minTickGap={isCompact ? 44 : 60}
          />
          <YAxis
            className="text-xs"
            axisLine={false}
            tickLine={false}
            tick={{ fill: 'hsl(var(--muted-foreground))' }}
            tickFormatter={formatYAxisTick}
            domain={yAxisConfig.domain}
            ticks={yAxisConfig.ticks}
            // Measured, not fixed: "€12k" and "CHF 120k" differ by half the axis.
            width="auto"
          />
          <Tooltip
            contentStyle={CHART_TOOLTIP_STYLE}
            // `chartData` writes `null` for a benchmark with no bar on a date; a `null`
            // return is what makes Recharts drop that row instead of printing "€0.00"
            // for a line that was not drawn. See `tooltipValue`.
            formatter={(value: number | undefined) => tooltipValue(value, formatCurrency)}
          />
          {series.map(s =>
            !s.show ? null
            : s.color === VALUE_COLOR ? (
              // The headline series is the one area, filled down to the axis; every other
              // series is a plain line read against it.
              <Area
                key={s.key}
                type="monotone"
                dataKey={s.key}
                stroke={s.color}
                strokeWidth={2.25}
                fill="url(#portfolio-value-fill)"
                name={s.name}
                dot={false}
                activeDot={{ r: 4, strokeWidth: 0 }}
              />
            ) : (
              <Line
                key={s.key}
                type="monotone"
                dataKey={s.key}
                stroke={s.color}
                strokeWidth={1.75}
                name={s.name}
                dot={false}
                activeDot={{ r: 4, strokeWidth: 0 }}
              />
            )
          )}
          {availableBenchmarks.map(b =>
            visibleBenchmarks.has(b.key) ? (
              <Line
                key={b.key}
                type="monotone"
                dataKey={`bench_${b.key}`}
                stroke={b.color}
                strokeWidth={1.75}
                strokeDasharray="5 5"
                name={b.name}
                dot={false}
                activeDot={{ r: 4, strokeWidth: 0 }}
                connectNulls
              />
            ) : null
          )}
        </ComposedChart>
      </ResponsiveContainer>
      </div>
    </div>
  )
}
