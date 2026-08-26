import { useState, useMemo, useEffect, useRef } from 'react'
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from 'recharts'
import type { PortfolioValuePoint, BenchmarkValuePoint } from '@/lib/api'
import { formatDate, parseLocalDate } from '@/lib/utils'
import { useFormatCurrency, useCurrencySymbol } from '@/lib/CurrencyContext'
import { useIsCompact } from '@/lib/useMediaQuery'
import { axisFloor, niceTicks } from '@/lib/niceTicks'
import { cashCaveat, cashIsTracked } from '@/lib/portfolioCash'

/**
 * One height for the chart and its three placeholder states, so a range switch or a
 * fetch failure cannot make the page jump. 600px was the only value at every width,
 * which is 71% of an 844px phone screen.
 */
const CHART_BOX = 'h-[280px] sm:h-[420px] lg:h-[600px]'

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
  const caveat = useMemo(
    () => cashCaveat(data?.[data.length - 1]?.cash_source),
    [data]
  )

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
   */
  const series = useMemo(() => {
    const baseline = cashTracked
      ? { key: 'money_in_eur', name: 'Money In' }
      : { key: 'cost_basis_eur', name: 'Cost Basis' }
    const value = cashTracked
      ? { key: 'total_value_eur', name: 'Total Value' }
      : { key: 'market_value_eur', name: 'Market Value' }
    return [
      { ...baseline, color: '#8b5cf6', show: showBaseline, toggle: () => setShowBaseline(v => !v) },
      { ...value, color: '#22c55e', show: showValue, toggle: () => setShowValue(v => !v) },
      ...(cashTracked
        ? [{ key: 'cash_eur', name: 'Cash', color: '#38bdf8', show: showCash, toggle: () => setShowCash(v => !v) }]
        : []),
      { key: 'profit_eur', name: 'Profit/Loss', color: '#f59e0b', show: showProfit, toggle: () => setShowProfit(v => !v) },
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

    return data.map(point => {
      // Profit follows whichever pair is on screen, so the third line is always the
      // gap between the other two rather than a quantity of its own.
      const profit = cashTracked
        ? (point.total_value_eur ?? 0) - (point.money_in_eur ?? 0)
        : point.market_value_eur - point.cost_basis_eur
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

  /**
   * X tick labels.
   *
   * This used to return a label only on the 1st of a month and `''` otherwise. That
   * defeats recharts' own thinning, which drops ticks by *index*: over two years it
   * kept up to 24 full-length labels while the ticks between them were empty strings,
   * so `minTickGap` had nothing to measure and the labels collided.
   *
   * Every tick is labelled now and recharts thins by measured text width. Consequence
   * to expect on desktop too: ticks no longer land on month firsts. The conservative
   * alternative — keep the day-1 rule, raise `interval` — does not fix 2Y at 390px, so
   * it does not fix the problem.
   */
  const formatXAxisTick = (value: string) => {
    // `parseLocalDate`, not `new Date(value)`. The API speaks date-only strings, which
    // `Date` parses as **UTC midnight** — so for any viewer west of Greenwich the point
    // dated 2026-01-01 becomes 2025-12-31 local and this tick prints "Dec 25" while the
    // tooltip for the very same point, which already goes through `formatDate` ->
    // `parseLocalDate`, prints "Jan 1, 2026". Axis and tooltip disagreeing on one screen,
    // and every month-boundary tick naming the wrong month. `utils.ts` exists for exactly
    // this and this was its last unconverted call site.
    const date = parseLocalDate(value)
    if (Number.isNaN(date.getTime())) return ''
    return date.toLocaleDateString('en-US', { month: 'short', year: '2-digit' })
  }

  // Custom tick formatter for Y axis
  const formatYAxisTick = (value: number) => {
    if (value === 0) return `${curSym}0`
    const absValue = Math.abs(value)
    if (absValue >= 1000) {
      return `${value < 0 ? '-' : ''}${curSym}${(absValue / 1000).toFixed(0)}k`
    }
    return `${curSym}${value.toFixed(0)}`
  }

  // Calculate dynamic Y axis domain and ticks based on data and visible lines
  const yAxisConfig = useMemo(() => {
    if (!chartData || chartData.length === 0) {
      return {
        domain: [0, 50000] as [number, number],
        ticks: [0, 10000, 20000, 30000, 40000, 50000]
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
        ticks: [0, 10000, 20000, 30000, 40000, 50000]
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
            The backend didn't respond — it may be redeploying. It retries automatically.
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
      {incomplete.days > 0 && (
        <div
          role="alert"
          className="rounded-md border border-yellow-600/40 bg-yellow-600/10 px-3 py-2 text-xs text-yellow-700 dark:text-yellow-500"
        >
          <span className="font-medium">
            {incomplete.days} {incomplete.days === 1 ? 'day' : 'days'} in this range could not
            be fully valued
          </span>{' '}
          — up to {incomplete.worst} {incomplete.worst === 1 ? 'holding' : 'holdings'} had no
          usable price{incomplete.from ? `, from ${incomplete.from}` : ''}. Market value and
          profit are understated on those days by the whole value of the missing holdings, so
          the dip is a gap in the price data rather than a loss. Risk metrics skip them.
        </div>
      )}

      {cashTracked && caveat && (
        <p className="text-xs text-muted-foreground">
          <span className="font-medium text-foreground">Total Value</span> is holdings plus
          uninvested cash, and <span className="font-medium text-foreground">Money In</span> is
          what you contributed — so a sale moves value between the lines instead of off the
          chart, and the gap between them is total profit. Cash is {caveat}.
        </p>
      )}

      {/* Toggle Buttons. Already wrapping; the min-height is the touch target. */}
      <div className="flex gap-2 flex-wrap items-center sm:gap-3 [&>button]:min-h-11 sm:[&>button]:min-h-0">
        <span className="text-sm text-muted-foreground">Show:</span>
        {series.map(s => (
          <button
            key={s.key}
            onClick={s.toggle}
            className={`flex items-center gap-2 text-sm px-3 py-1.5 rounded-md transition-all ${
              s.show ? 'font-medium' : 'text-muted-foreground hover:text-foreground hover:bg-muted/50'
            }`}
            style={s.show ? { backgroundColor: `${s.color}1a`, color: s.color } : undefined}
          >
            <div
              className="w-2.5 h-2.5 rounded-full"
              style={{ backgroundColor: s.show ? s.color : 'hsl(var(--muted-foreground) / 0.3)' }}
            />
            {s.name}
          </button>
        ))}
        {availableBenchmarks.map(b => (
          <button
            key={b.key}
            onClick={() => toggleBenchmark(b.key)}
            className={`flex items-center gap-2 text-sm px-3 py-1.5 rounded-md transition-all ${
              visibleBenchmarks.has(b.key)
                ? `font-medium`
                : 'text-muted-foreground hover:text-foreground hover:bg-muted/50'
            }`}
            style={visibleBenchmarks.has(b.key) ? { backgroundColor: `${b.color}15`, color: b.color } : undefined}
          >
            <div className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: visibleBenchmarks.has(b.key) ? b.color : 'hsl(var(--muted-foreground) / 0.3)' }} />
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
        <LineChart data={chartData} margin={{ top: 5, right: isCompact ? 8 : 30, left: 0, bottom: 5 }}>
          <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
          <XAxis
            dataKey="date"
            className="text-xs"
            tick={{ fill: 'hsl(var(--muted-foreground))' }}
            tickFormatter={formatXAxisTick}
            interval="preserveStartEnd"
            // Every tick is labelled now, so recharts can thin by measured text width.
            // See formatXAxisTick for why the old day-1 rule made that impossible.
            minTickGap={isCompact ? 44 : 60}
          />
          <YAxis
            className="text-xs"
            tick={{ fill: 'hsl(var(--muted-foreground))' }}
            tickFormatter={formatYAxisTick}
            domain={yAxisConfig.domain}
            ticks={yAxisConfig.ticks}
            // "€12k" is ~34px; the default 60 spends a fifth of a phone's plot area.
            width={isCompact ? 44 : 60}
          />
          <Tooltip
            contentStyle={{
              backgroundColor: 'hsl(var(--card))',
              border: '1px solid hsl(var(--border))',
              borderRadius: '8px',
              // Recharts clamps a tooltip to the viewBox, so it never causes overflow —
              // but unbounded it fills the whole plot area at 390px.
              maxWidth: '70vw',
            }}
            formatter={(value: number | undefined) => value !== undefined ? formatCurrency(value) : ''}
          />
          {series.map(s =>
            s.show ? (
              <Line
                key={s.key}
                type="monotone"
                dataKey={s.key}
                stroke={s.color}
                strokeWidth={2}
                name={s.name}
                dot={false}
                activeDot={{ r: 6 }}
              />
            ) : null
          )}
          {availableBenchmarks.map(b =>
            visibleBenchmarks.has(b.key) ? (
              <Line
                key={b.key}
                type="monotone"
                dataKey={`bench_${b.key}`}
                stroke={b.color}
                strokeWidth={2}
                strokeDasharray="5 5"
                name={b.name}
                dot={false}
                activeDot={{ r: 6 }}
                connectNulls
              />
            ) : null
          )}
        </LineChart>
      </ResponsiveContainer>
      </div>
    </div>
  )
}
