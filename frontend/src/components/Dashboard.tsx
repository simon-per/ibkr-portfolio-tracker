import { useState, useMemo, lazy } from 'react'
import { useQuery, useQueries, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { BenchmarkDataset } from './PortfolioValueChart'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { LazyTabPanel } from '@/components/ui/LazyTabPanel'
import { PortfolioValueChart } from './PortfolioValueChart'
import { PortfolioSummaryCards } from './PortfolioSummaryCards'
import { PerformanceMetricsCards } from './PerformanceMetricsCards'
import { RiskMetricsCards } from './RiskMetricsCards'
import { ContributionsStrip } from './ContributionsStrip'
import { PositionsList } from './PositionsList'
import { PerformanceAttribution } from './PerformanceAttribution'
import { MonthlyReturnsHeatmap } from './MonthlyReturnsHeatmap'
import { MonthlyDeploymentCard } from './MonthlyDeploymentCard'
import { DividendSummary } from './DividendSummary'
/**
 * The seven non-default tabs load on demand; the Performance tab does not.
 *
 * The build was one 891 kB chunk and Vite warned on it every time. Recharts is most of
 * that weight, but splitting *it* out wins little — `PortfolioValueChart`,
 * `PerformanceAttribution` and `MonthlyDeploymentCard` all sit on the default
 * Performance tab, so the charting library is needed at first paint regardless. What is
 * genuinely deferrable is the other seven panels' own code, which nobody has asked for
 * when the page loads.
 *
 * Safe because `TabsContent` returns null while inactive, so a lazy panel is not
 * mounted (and therefore not fetched) until its tab is actually selected.
 */
const ActivityTab = lazy(() => import('./ActivityTab').then(m => ({ default: m.ActivityTab })))
const AllocationTab = lazy(() => import('./AllocationTab').then(m => ({ default: m.AllocationTab })))
const ForecastTab = lazy(() => import('./ForecastTab').then(m => ({ default: m.ForecastTab })))
const FundamentalsTab = lazy(() => import('./FundamentalsTab').then(m => ({ default: m.FundamentalsTab })))
const WatchlistTab = lazy(() => import('./WatchlistTab').then(m => ({ default: m.WatchlistTab })))
const TaxTab = lazy(() => import('./TaxTab').then(m => ({ default: m.TaxTab })))
const DividendsTab = lazy(() => import('./DividendsTab').then(m => ({ default: m.DividendsTab })))
const LookThroughTab = lazy(() => import('./LookThroughTab').then(m => ({ default: m.LookThroughTab })))
import { ThemeToggle } from './ThemeToggle'
import { AdminKeyButton } from './AdminKeyButton'
import { SyncStatusMessage } from './SyncStatusMessage'
import { BenchmarkPicker, BENCHMARK_COLORS } from './BenchmarkPicker'
import { useBaseCurrency, useCurrencySymbol } from '@/lib/CurrencyContext'
import {
  MIN_PAIRED_RETURNS,
  annualizedVolatilityPct,
  benchmarkAsValueSeries,
  betaAndCorrelation,
  concentrationPct,
  drawdownDetail,
  herfindahlConcentration,
  maxDrawdownPct,
  sharpeRatio,
  sortinoRatio,
} from '@/lib/portfolioKpis'
import { rangeFor, TIME_RANGES, type TimeRange } from '@/lib/dateRanges'
import { RefreshCw, Download, Clock } from 'lucide-react'

const BENCHMARKS_KEY = 'selectedBenchmarks'

/**
 * The stored benchmark selection, validated rather than trusted.
 *
 * `JSON.parse` was already wrapped in a try/catch, which covers malformed JSON but
 * not *well-formed JSON of the wrong shape*: `42` and `{"a":1}` both parse cleanly
 * and were then handed back as `string[]`. `selectedBenchmarks.map(...)` feeds
 * `useQueries` a few lines below, so a non-array throws inside this component — the
 * whole dashboard, not one tab — and because the bad value is re-read on every mount,
 * reloading cannot recover it. Clearing site data would be the only way out.
 *
 * Same shape and reasoning as `RebalanceCard`'s `readTargets`, which drops entries it
 * cannot use instead of coercing them. Non-string members go too: they would reach the
 * benchmark query as keys and fetch nothing.
 */
export function readSelectedBenchmarks(): string[] {
  try {
    const raw = localStorage.getItem(BENCHMARKS_KEY)
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter((k): k is string => typeof k === 'string')
  } catch {
    return []
  }
}

export function Dashboard() {
  const queryClient = useQueryClient()
  const {
    baseCurrency, supportedCurrencies, setBaseCurrency,
    isUpdating: currencyUpdating, updateError: currencyError, currencyIsAssumed,
  } = useBaseCurrency()
  const curSym = useCurrencySymbol()
  const [selectedRange, setSelectedRange] = useState<TimeRange>('1Y')
  const [selectedBenchmarks, setSelectedBenchmarks] = useState<string[]>(readSelectedBenchmarks)

  const handleBenchmarkChange = (keys: string[]) => {
    setSelectedBenchmarks(keys)
    localStorage.setItem(BENCHMARKS_KEY, JSON.stringify(keys))
  }

  // Fetch average monthly contributions. Declared before `dateRange` because ALL
  // starts at the portfolio's real inception, which this response carries
  // (`first_contribution_date` = min(taxlots.open_date)). Transferred lots keep their
  // original open_date, so a hardcoded inception goes stale the moment an older
  // statement is ingested.
  const {
    data: contributions,
    isLoading: contributionsLoading,
    isError: contributionsError,
  } = useQuery({
    queryKey: ['portfolio', 'contributions'],
    queryFn: () => api.getContributions(),
    staleTime: 30 * 60 * 1000,
  })

  const inception = contributions?.first_contribution_date ?? null

  // Every boundary is a LOCAL calendar date. Serialising local midnight through
  // toISOString() made YTD/MTD start a day early in any positive-UTC-offset zone —
  // same portfolio, different numbers by locale. See lib/dateRanges.ts.
  const dateRange = useMemo(
    () => rangeFor(selectedRange, new Date(), inception),
    [selectedRange, inception]
  )

  // Fetch portfolio summary
  const { data: summary, isLoading: summaryLoading, isError: summaryError } = useQuery({
    queryKey: ['portfolio', 'summary'],
    queryFn: () => api.getPortfolioSummary(),
    staleTime: 30 * 60 * 1000,
  })

  // Fetch portfolio value over time
  const { data: valueOverTime, isLoading: chartLoading, isError: chartError } = useQuery({
    queryKey: ['portfolio', 'value-over-time', dateRange],
    queryFn: () => api.getPortfolioValueOverTime(dateRange.start, dateRange.end),
    staleTime: 30 * 60 * 1000,
  })

  // Fetch positions
  const { data: positions, isLoading: positionsLoading, isError: positionsError } = useQuery({
    queryKey: ['portfolio', 'positions'],
    queryFn: () => api.getPositions(),
  })

  // Fetch benchmark comparisons (dynamic based on selection)
  const benchmarkQueries = useQueries({
    queries: selectedBenchmarks.map((key) => ({
      queryKey: ['portfolio', 'benchmark', dateRange, key],
      queryFn: () => api.getBenchmarkComparison(dateRange.start, dateRange.end, key),
      enabled: !!dateRange.start && !!dateRange.end,
      staleTime: 30 * 60 * 1000,
    })),
  })

  const benchmarkDatasets: BenchmarkDataset[] = useMemo(() => {
    return selectedBenchmarks
      .map((key, i) => {
        const query = benchmarkQueries[i]
        if (!query?.data) return null
        return {
          key,
          name: query.data.benchmark_name,
          color: BENCHMARK_COLORS[i % BENCHMARK_COLORS.length],
          data: query.data.data,
        }
      })
      .filter((d): d is BenchmarkDataset => d !== null)
  }, [selectedBenchmarks, benchmarkQueries])

  // Fetch XIRR annualized return for selected time range
  const { data: annualizedReturn, isLoading: xirrLoading } = useQuery({
    queryKey: ['portfolio', 'annualized-return', dateRange],
    queryFn: () => api.getAnnualizedReturn(dateRange.start, dateRange.end),
    enabled: !!dateRange.start && !!dateRange.end,
    staleTime: 30 * 60 * 1000,
  })

  // Fetch performance attribution for selected time range
  const { data: attribution, isLoading: attributionLoading, isError: attributionError } = useQuery({
    queryKey: ['portfolio', 'attribution', dateRange],
    queryFn: () => api.getPerformanceAttribution(dateRange.start, dateRange.end),
    enabled: !!dateRange.start && !!dateRange.end,
    staleTime: 30 * 60 * 1000,
  })

  // The two dividend-yield cards. `/breakdown` reads only cached data — unlike
  // `/summary` it never enqueues a sync, so it cannot reach Yahoo.
  //
  // Keyed on the CURRENT YEAR to match `DividendsTab`'s own default query exactly
  // (`['dividends','breakdown', year]` with `year` initialised to the current one), so
  // opening that tab costs no second request. Safe only because `forward_yield` and
  // `growth` are unwindowed — the same numbers whichever year is asked for, which
  // `test_api_smoke.py` pins from both ends.
  const {
    data: dividendBreakdown,
    isError: dividendBreakdownError,
  } = useQuery({
    queryKey: ['dividends', 'breakdown', new Date().getFullYear()],
    queryFn: () => api.getDividendBreakdown(new Date().getFullYear()),
    staleTime: 30 * 60 * 1000,
  })

  // Per-security yield on cost for the positions table, keyed on `security_id` because
  // identity is isin + exchange (ASML is two securities). Memoised so the table's column
  // factory and sort comparator do not see a fresh Map identity on every render.
  //
  // Securities with no payments and no projection are simply absent — the breakdown only
  // carries the ones that have something to say — which the table renders as a dash.
  const yieldOnCostBySecurity = useMemo(
    () => new Map(
      (dividendBreakdown?.securities ?? []).map((r) => [r.security_id, r.yield_on_cost_pct]),
    ),
    [dividendBreakdown],
  )

  // Fetch scheduler status (poll every 60s)
  const { data: schedulerStatus } = useQuery({
    queryKey: ['scheduler', 'status'],
    queryFn: () => api.getSchedulerStatus(),
    refetchInterval: 60_000,
  })

  // Which build is live, whether the scheduler is armed, and whether writes are
  // protected. Rendered in the footer so "did my deploy land" is answerable from the
  // browser instead of over ssh. Refetched on mount only — it changes on deploy.
  const { data: health } = useQuery({
    queryKey: ['health'],
    queryFn: () => api.healthCheck(),
    staleTime: Infinity,
  })

  // Calculate performance metrics for selected timeframe
  const performanceMetrics = useMemo(() => {
    if (!valueOverTime || valueOverTime.length === 0) {
      return null
    }

    const firstPoint = valueOverTime[0]
    const lastPoint = valueOverTime[valueOverTime.length - 1]

    const startValue = firstPoint.market_value_eur
    const currentValue = lastPoint.market_value_eur
    const absoluteChange = currentValue - startValue
    const percentageChange = startValue > 0 ? (absoluteChange / startValue) * 100 : 0

    // Period gain. The attribution endpoint already computes the period's
    // economic P&L over the same range — value change plus disposal proceeds
    // minus new investment — so it counts a realized gain. The local fallback
    // is the change in UNREALIZED profit, which drops when a winner is sold:
    // the gain leaves the unrealized pool and shows up nowhere.
    const startProfit = firstPoint.market_value_eur - firstPoint.cost_basis_eur
    const currentProfit = lastPoint.market_value_eur - lastPoint.cost_basis_eur
    const periodGain = attribution?.total_pnl_eur ?? (currentProfit - startProfit)
    // Use cost basis as denominator for gain % (more meaningful than profit-on-profit)
    const startCostBasis = firstPoint.cost_basis_eur
    const periodGainPercent = startCostBasis > 0 ? (periodGain / startCostBasis) * 100 : 0

    return {
      startValue,
      currentValue,
      absoluteChange,
      percentageChange,
      startDate: firstPoint.date,
      endDate: lastPoint.date,
      periodGain,
      periodGainPercent,
    }
  }, [valueOverTime, attribution])

  // Calculate KPIs
  const kpiMetrics = useMemo(() => {
    if (!valueOverTime || valueOverTime.length < 2 || !positions) {
      return null
    }

    // 1. Annual Return (XIRR) - from backend, fallback to null
    const xirr = annualizedReturn?.annualized_return_pct ?? null
    // "simple_period" for <30-day windows: a raw period return, not annualized.
    const xirrMethod = annualizedReturn?.method ?? 'xirr'

    // 2/3. Drawdown and Sharpe, from flow-adjusted daily returns. Extracted and
    // unit-tested: both net out the day's external flow, and inferring that flow
    // from the cost-basis line booked every profitable sale as a loss.
    const maxDrawdown = maxDrawdownPct(valueOverTime)
    const sharpe = sharpeRatio(valueOverTime)

    // 4. Win Rate (percentage of profitable positions)
    const profitablePositions = positions.filter(p => p.gain_loss_eur > 0).length
    const winRate = positions.length > 0 ? (profitablePositions / positions.length) * 100 : 0

    // 5. Calmar Ratio (XIRR / |Max Drawdown|) — needs an ANNUALIZED numerator,
    // so it goes blank on short ranges where only a period return exists.
    const calmarRatio = xirr !== null && xirrMethod === 'xirr' && maxDrawdown < 0
      ? xirr / Math.abs(maxDrawdown)
      : null

    // 6. Top 5 Concentration, footnoted with the effective holdings it cannot express:
    // a top-5 weight reads the same for five equal positions and one dominant one.
    const top5Weight = concentrationPct(positions, 5)
    const { effectiveHoldings } = herfindahlConcentration(positions)

    return {
      xirr,
      xirrMethod,
      maxDrawdown,
      sharpeRatio: sharpe,
      winRate,
      profitablePositions,
      totalPositions: positions.length,
      calmarRatio,
      top5Weight,
      effectiveHoldings,
    }
  }, [valueOverTime, positions, annualizedReturn])

  // The risk side of the same series. Kept in a second memo because beta also
  // depends on the benchmark selection, which the return KPIs do not.
  const riskMetrics = useMemo(() => {
    if (!valueOverTime || valueOverTime.length < 2 || !positions) {
      return null
    }

    const drawdown = drawdownDetail(valueOverTime)

    // Beta uses the FIRST selected benchmark: it is the primary comparison and
    // the one the chart draws first. Nothing selected means no beta rather than
    // a silent default, because which index the portfolio is measured against
    // changes the answer.
    const primary = benchmarkDatasets[0]
    const beta = primary
      ? {
          ...betaAndCorrelation(valueOverTime, benchmarkAsValueSeries(primary.data)),
          minSampleDays: MIN_PAIRED_RETURNS,
          benchmarkName: primary.name,
        }
      : null

    return {
      volatilityPct: annualizedVolatilityPct(valueOverTime),
      sortino: sortinoRatio(valueOverTime),
      currentDrawdownPct: drawdown.currentDrawdownPct,
      maxDrawdownPct: drawdown.maxDrawdownPct,
      troughDate: drawdown.troughDate,
      recoveredDate: drawdown.recoveredDate,
      beta,
    }
  }, [valueOverTime, positions, benchmarkDatasets])

  // Sync mutation
  const syncMutation = useMutation({
    mutationFn: (force: boolean = false) => api.syncIBKRData(force),
    onSuccess: (data) => {
      // A skipped sync fetched nothing, so there is nothing to invalidate — and
      // refetching the whole portfolio to display "already up to date" would make the
      // cheapest outcome the most expensive one.
      if (data.status === 'skipped') return
      // Invalidate all queries to refresh data
      queryClient.invalidateQueries({ queryKey: ['portfolio'] })
    },
  })

  const handleSync = () => {
    syncMutation.mutate(false)
  }

  return (
    <div className="min-h-screen bg-background">
      {/* Header */}
      <div className="border-b">
        <div className="w-full px-4 py-3 sm:py-4">
          {/* Wraps below `sm`: the title block and the four controls cannot share a
              358px row, and `justify-between` on a row that cannot wrap is what pushed
              the cluster past the viewport edge. `items-start` so the controls sit
              level with the title rather than with the bottom of the status block. */}
          <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
            <div className="min-w-0">
              <h1 className="text-xl font-bold tracking-tight sm:text-3xl">Portfolio Analyzer</h1>
              {/* Hidden below `sm`: the strapline costs a line of an 844px screen and
                  tells a returning user nothing they do not already know. */}
              <p className="text-muted-foreground mt-1 hidden sm:block">
                Track your IBKR portfolio with cost basis and market value
              </p>
              {schedulerStatus && schedulerStatus.status === 'running' && (
                // Short timestamps at every width, not short only on phones: two full
                // toLocaleString() values are ~50 characters each, and "8/2/26, 8:00 AM"
                // answers "did the sync run" just as well on a desktop. One format,
                // nothing to drift.
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-2 text-xs text-muted-foreground">
                  <Clock className="h-3 w-3 shrink-0" />
                  {schedulerStatus.last_sync ? (
                    <span>
                      Last sync: {new Date(schedulerStatus.last_sync.timestamp).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' })} ({schedulerStatus.last_sync.status})
                    </span>
                  ) : (
                    <span>No sync has run yet</span>
                  )}
                  {schedulerStatus.jobs.length > 0 && schedulerStatus.jobs[0].next_run_time && (
                    <span>
                      · Next: {new Date(schedulerStatus.jobs[0].next_run_time).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' })}
                    </span>
                  )}
                </div>
              )}
              {/* A bare "(error)" isn't actionable — show what actually went wrong. */}
              {schedulerStatus?.last_sync?.status === 'error' && schedulerStatus.last_sync.message && (
                <p className="mt-1 max-w-3xl text-xs text-amber-700 dark:text-amber-400">
                  {schedulerStatus.last_sync.message}
                </p>
              )}
              {/* Warnings ride on SUCCESSFUL runs (stale prices, skipped lots,
                  reclassified transfers) — they were computed and persisted but
                  never rendered anywhere, which is how a mispriced position once
                  went unnoticed for months. */}
              {(schedulerStatus?.last_sync?.warnings?.length ?? 0) > 0 && (
                <details className="mt-1 max-w-3xl">
                  <summary className="cursor-pointer text-xs font-medium text-amber-700 dark:text-amber-400">
                    ⚠ {schedulerStatus?.last_sync?.warnings?.length} warning
                    {(schedulerStatus?.last_sync?.warnings?.length ?? 0) > 1 ? 's' : ''} from the
                    last sync
                  </summary>
                  <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs text-amber-700/90 dark:text-amber-400/90">
                    {(schedulerStatus?.last_sync?.warnings ?? []).map((w, i) => (
                      <li key={i}>{w}</li>
                    ))}
                  </ul>
                </details>
              )}
            </div>
            {/* Wraps: at 390px the currency select plus the sync button are
                wider than the viewport, and without this the whole page scrolled
                horizontally by ~25px on every tab. */}
            <div className="flex flex-wrap items-center justify-end gap-2">
              <select
                value={baseCurrency}
                onChange={(e) => setBaseCurrency(e.target.value)}
                disabled={currencyUpdating}
                title="Base currency"
                className="h-9 rounded-md border border-input bg-background px-3 text-sm font-medium disabled:opacity-50"
              >
                {supportedCurrencies.map((c) => (
                  <option key={c} value={c}>{c}</option>
                ))}
              </select>
              {currencyError && (
                <span className="max-w-[12rem] text-xs leading-tight text-red-600 dark:text-red-400" role="alert">
                  {currencyError}
                </span>
              )}
              {/* Without this the app labels every figure `€` on a failed settings
                  fetch while the numbers behind them are whatever the account
                  actually uses — and `staleTime: Infinity` makes that stick for the
                  session rather than blink. */}
              {currencyIsAssumed && !currencyError && (
                <span className="max-w-[12rem] text-xs leading-tight text-amber-700 dark:text-amber-400" role="alert">
                  Couldn't read your display currency — showing {baseCurrency}, which may not be it.
                </span>
              )}
              <ThemeToggle />
              <AdminKeyButton writeAuthEnabled={health?.write_auth_enabled} />
              {/* The label is hidden below `sm`, not the button, and not moved into an
                  overflow menu — that would be a second rendering of a four-control
                  set. The icon plus this name carry the same meaning to a screen
                  reader as the visible label does. */}
              <Button
                onClick={handleSync}
                disabled={syncMutation.isPending}
                variant="outline"
                className="px-3 sm:px-4"
                aria-label={syncMutation.isPending ? 'Syncing IBKR data' : 'Sync IBKR data'}
              >
                {syncMutation.isPending ? (
                  <RefreshCw className="h-4 w-4 animate-spin sm:mr-2" />
                ) : (
                  <Download className="h-4 w-4 sm:mr-2" />
                )}
                <span className="hidden sm:inline">
                  {syncMutation.isPending ? 'Syncing...' : 'Sync IBKR Data'}
                </span>
              </Button>
            </div>
          </div>

          {/* Sync status messages — see SyncStatusMessage for why the skip state is
              neutral rather than red. */}
          <SyncStatusMessage
            data={syncMutation.isSuccess ? syncMutation.data : undefined}
            error={syncMutation.isError ? syncMutation.error : null}
            isPending={syncMutation.isPending}
            onForce={() => syncMutation.mutate(true)}
          />
        </div>
      </div>

      {/* Main Content */}
      <div className="w-full px-4 py-4 sm:py-6">
        <Tabs defaultValue="performance" className="space-y-6 sm:space-y-8">
          {/* Sticky and full-bleed below `sm`, exactly today's eight-column grid above
              it.

              Eight sections do not fit in 358px — each cell was ~53px while the
              triggers are whitespace-nowrap, so the labels overlapped into a smear —
              so the strip scrolls. It runs to the screen edge (`-mx-4` here, the gutter
              restored as `px-4` *inside* the scroller) so the next pill is visibly
              half-cut: that is the "there is more" cue, and unlike an edge fade it
              cannot lie about which end still has content. It stays put while a
              3,000px panel scrolls under it because it is the one control used
              constantly on a phone.

              `p-0` before `px-4 py-2` so tailwind-merge drops TabsList's base `p-1`
              outright rather than the result depending on Tailwind's emit order. */}
          <div className="sticky top-0 z-30 -mx-4 border-b bg-background sm:static sm:mx-0 sm:border-0 sm:bg-transparent">
            <TabsList
              label="Portfolio sections"
              // `justify-start` matters and is not cosmetic: TabsList's base is
              // `justify-center`, and a centred flex row wider than its scroller
              // overflows equally on BOTH sides — the leading overflow is unreachable
              // because scrollLeft cannot go negative. With eight tabs in 358px that
              // put "Performance" ~150px off the left edge, permanently untappable.
              className="flex w-full justify-start gap-1 overflow-x-auto rounded-none p-0 px-4 py-2 scroll-px-4 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden sm:grid sm:max-w-4xl sm:auto-cols-fr sm:grid-flow-col sm:gap-0 sm:rounded-md sm:p-1"
            >
              <TabsTrigger value="performance">Performance</TabsTrigger>
              <TabsTrigger value="activity">Activity</TabsTrigger>
              <TabsTrigger value="allocation">Allocation</TabsTrigger>
              <TabsTrigger value="lookthrough">Look-through</TabsTrigger>
              <TabsTrigger value="dividends">Dividends</TabsTrigger>
              <TabsTrigger value="fundamentals">Fundamentals</TabsTrigger>
              <TabsTrigger value="watchlist">Watchlist</TabsTrigger>
              <TabsTrigger value="forecast">Forecast</TabsTrigger>
              <TabsTrigger value="tax">Tax</TabsTrigger>
            </TabsList>
          </div>

          {/* Performance Tab */}
          <TabsContent value="performance" className="space-y-8">
            {/* Summary Cards */}
            <PortfolioSummaryCards
              summary={summary}
              isLoading={summaryLoading}
              isError={summaryError}
              periodChangePct={performanceMetrics?.percentageChange ?? null}
              periodLabel={selectedRange}
            />

            {/* Performance Metrics - Time-Filtered KPIs */}
            <PerformanceMetricsCards
              metrics={kpiMetrics}
              isLoading={chartLoading || positionsLoading || xirrLoading}
              isError={chartError || positionsError}
            />

            {/* Risk Metrics — the denominators the row above divides by, plus the two
                dividend rates. `isLoading` deliberately excludes the dividend query:
                gating this row on it would hide four already-computed metrics behind a
                third request, and the two cards state their own absence instead. */}
            <RiskMetricsCards
              metrics={riskMetrics}
              dividend={dividendBreakdown?.forward_yield}
              dividendError={dividendBreakdownError}
              isLoading={chartLoading || positionsLoading}
              isError={chartError || positionsError}
            />

            {/* Portfolio Value Chart */}
            <Card>
              <CardHeader>
                {/* Stacks below `sm`. Nine range buttons come to ~465px and the
                    benchmark picker another 40 — against ~324px of card interior at
                    390px, which is what put the whole page into horizontal scroll. */}
                <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
                  <div className="min-w-0">
                    <CardTitle>Portfolio Value Over Time</CardTitle>
                    <CardDescription>
                      Cost basis (invested) vs Market value (current worth) in {baseCurrency}
                    </CardDescription>
                  </div>
                  <div className="flex min-w-0 items-center gap-2">
                    {/* `min-w-0` is load-bearing: without it this flex child refuses to
                        shrink below its content width and overflows the card whatever
                        `overflow-x-auto` says. All nine ranges stay reachable — a
                        shortened mobile set would need a second control *and* a second
                        copy of TIME_RANGES. Snap points because the pills are
                        near-uniform width; the tab strip deliberately does not snap,
                        because its labels are not. */}
                    <div className="flex min-w-0 flex-1 snap-x snap-mandatory gap-1 overflow-x-auto scroll-px-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden sm:flex-none sm:snap-none sm:overflow-visible">
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
                    <BenchmarkPicker
                      selected={selectedBenchmarks}
                      onChange={handleBenchmarkChange}
                    />
                  </div>
                </div>
                {/* Period metrics on the left, average money added on the right — the
                    contributions strip lives here rather than in a card of its own, and
                    sits outside the performanceMetrics guard so it survives without them. */}
                <div className="mt-4 flex flex-wrap items-end justify-between gap-x-8 gap-y-3">
                {performanceMetrics && (
                  <div className="space-y-2">
                    <div className="flex items-center gap-6 text-sm">
                      {/* Wraps: label + currency + percent fits at 390px in EUR but not
                          with "CHF 12,345.67", and the three are one unbreakable run. */}
                      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                        <span
                          className="text-muted-foreground"
                          title="Change in total holdings value over the period. This includes money you added — it is not a return; see Period Gain for that."
                        >
                          Value change:
                        </span>
                        <span className={`font-semibold ${performanceMetrics.absoluteChange >= 0 ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}`}>
                          {performanceMetrics.absoluteChange >= 0 ? '+' : ''}{curSym}{performanceMetrics.absoluteChange.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                        </span>
                        <span className={`font-semibold ${performanceMetrics.percentageChange >= 0 ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}`}>
                          ({performanceMetrics.percentageChange >= 0 ? '+' : ''}{performanceMetrics.percentageChange.toFixed(2)}%)
                        </span>
                      </div>
                    </div>
                    <div className="flex items-center gap-6 text-sm">
                      {/* Wraps: label + currency + percent fits at 390px in EUR but not
                          with "CHF 12,345.67", and the three are one unbreakable run. */}
                      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                        <span
                          className="text-muted-foreground"
                          title="What the holdings actually earned over the period: value change plus proceeds of anything sold, less money put in. Realized gains count."
                        >
                          Period Gain:
                        </span>
                        <span className={`font-semibold ${performanceMetrics.periodGain >= 0 ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}`}>
                          {performanceMetrics.periodGain >= 0 ? '+' : ''}{curSym}{performanceMetrics.periodGain.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                        </span>
                        <span className={`font-semibold ${performanceMetrics.periodGainPercent >= 0 ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}`}>
                          ({performanceMetrics.periodGainPercent >= 0 ? '+' : ''}{performanceMetrics.periodGainPercent.toFixed(2)}%)
                        </span>
                      </div>
                    </div>
                  </div>
                )}
                  <ContributionsStrip data={contributions} isLoading={contributionsLoading} isError={contributionsError} />
                </div>
              </CardHeader>
              <CardContent>
                <PortfolioValueChart
                  data={valueOverTime || []}
                  benchmarks={benchmarkDatasets}
                  isLoading={chartLoading}
                  isError={chartError}
                />
              </CardContent>
            </Card>

            {/* Monthly Returns Heatmap */}
            <MonthlyReturnsHeatmap data={valueOverTime} isLoading={chartLoading} isError={chartError} />

            {/* Monthly Deployment (capital put to work per month) */}
            <MonthlyDeploymentCard data={contributions} isLoading={contributionsLoading} isError={contributionsError} />

            {/* Dividend Income Heatmap */}
            <DividendSummary />

            {/* Performance Attribution */}
            <PerformanceAttribution data={attribution} isLoading={attributionLoading} isError={attributionError} />

            {/* Positions Table. The yield map rides on the breakdown already fetched
                above for the KPI cards, so this costs no extra request. */}
            <PositionsList
              positions={positions || []}
              isLoading={positionsLoading}
              isError={positionsError}
              yieldOnCost={yieldOnCostBySecurity}
            />
          </TabsContent>

          {/* Activity Tab — the transaction ledger */}
          <TabsContent value="activity">
            <LazyTabPanel label="Activity"><ActivityTab /></LazyTabPanel>
          </TabsContent>

          {/* Allocation Tab */}
          <TabsContent value="allocation">
            <LazyTabPanel label="Allocation"><AllocationTab /></LazyTabPanel>
          </TabsContent>

          {/* Look-through Tab */}
          <TabsContent value="lookthrough">
            <LazyTabPanel label="Look-through"><LookThroughTab /></LazyTabPanel>
          </TabsContent>

          {/* Dividends Tab */}
          <TabsContent value="dividends">
            <LazyTabPanel label="Dividends"><DividendsTab /></LazyTabPanel>
          </TabsContent>

          {/* Fundamentals Tab */}
          <TabsContent value="fundamentals">
            <LazyTabPanel label="Fundamentals"><FundamentalsTab /></LazyTabPanel>
          </TabsContent>

          {/* Watchlist Tab */}
          <TabsContent value="watchlist">
            <LazyTabPanel label="Watchlist"><WatchlistTab /></LazyTabPanel>
          </TabsContent>

          {/* Forecast Tab */}
          <TabsContent value="forecast">
            <LazyTabPanel label="Forecast"><ForecastTab /></LazyTabPanel>
          </TabsContent>

          {/* Tax Tab */}
          <TabsContent value="tax">
            <LazyTabPanel label="Tax"><TaxTab /></LazyTabPanel>
          </TabsContent>
        </Tabs>
      </div>

      {/* Build identity. /health used to return only {"status":"healthy"}, so
          confirming a deploy had landed — or that the scheduler was armed at all —
          meant ssh'ing to the box. */}
      {health && (
        <footer className="border-t">
          {/* A flex row rather than inline spans with `ml-*`: those margins survive a
              wrap and indent the start of the next line. */}
          <div className="flex w-full flex-wrap items-center gap-x-2 gap-y-1 px-4 py-3 text-xs text-muted-foreground">
            <span>Portfolio Analyzer v{health.version}</span>
            {health.commit && health.commit !== 'unknown' && (
              <span className="font-mono">{health.commit.slice(0, 7)}</span>
            )}
            {/* Both call out a *disabled* safeguard, never a working one: a scheduler
                that has quietly stopped looks exactly like a healthy site. */}
            {!health.scheduler_enabled && (
              <span className="text-amber-700 dark:text-amber-400">
                · scheduler disabled — no automatic syncs
              </span>
            )}
            {!health.write_auth_enabled && (
              <span className="text-amber-700 dark:text-amber-400">
                · write API unauthenticated
              </span>
            )}
          </div>
        </footer>
      )}
    </div>
  )
}
