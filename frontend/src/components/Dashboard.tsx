import { useState, useMemo, lazy } from 'react'
import { useQuery, useQueries, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { BenchmarkDataset } from './PortfolioValueChart'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { LazyTabPanel } from '@/components/ui/LazyTabPanel'
import { PortfolioValueChart } from './PortfolioValueChart'
import { PortfolioHero } from './PortfolioHero'
import { SegmentedControl } from '@/components/ui/SegmentedControl'
import { PerformanceMetricsCards } from './PerformanceMetricsCards'
import { RiskMetricsCards } from './RiskMetricsCards'
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
// AnalyticsTab is hidden (owner, 2026-10-04): not clean enough and low value for him. The
// component, its tests and /api/performance/* stay, so restoring it is the lazy import, the
// trigger and the panel — see docs/performance-analytics.md.
import { AppHeader } from './AppHeader'
import { AppFooter } from './AppFooter'
import { SyncStatusMessage } from './SyncStatusMessage'
import { BenchmarkPicker } from './BenchmarkPicker'
import { benchmarkColor } from '@/lib/benchmarkColors'
import {
  MIN_PAIRED_RETURNS,
  annualizedVolatilityPct,
  benchmarkAsValueSeries,
  betaAndCorrelation,
  concentrationPct,
  drawdownDetail,
  herfindahlConcentration,
  maxDrawdownPct,
  periodChange,
  sharpeRatio,
  sortinoDetail,
  winRate,
} from '@/lib/portfolioKpis'
import { rangeFor, TIME_RANGES, type TimeRange } from '@/lib/dateRanges'
import { readStored, writeStored } from '@/lib/storage'
import { formatShortDateTime } from '@/lib/utils'
import { RefreshCw } from 'lucide-react'

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
    const raw = readStored(BENCHMARKS_KEY)
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
  const [selectedRange, setSelectedRange] = useState<TimeRange>('1Y')
  const [selectedBenchmarks, setSelectedBenchmarks] = useState<string[]>(readSelectedBenchmarks)

  const handleBenchmarkChange = (keys: string[]) => {
    setSelectedBenchmarks(keys)
    writeStored(BENCHMARKS_KEY, JSON.stringify(keys))
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

  // The full `/benchmarks` list, for colouring by identity rather than by position in
  // the selection. Same key and `staleTime` as `BenchmarkPicker`'s query, so it is one
  // request shared through the cache.
  const { data: availableBenchmarks } = useQuery({
    queryKey: ['portfolio', 'benchmarks'],
    queryFn: () => api.getAvailableBenchmarks(),
    staleTime: Infinity,
  })

  const benchmarkDatasets: BenchmarkDataset[] = useMemo(() => {
    return selectedBenchmarks
      .map((key, i): BenchmarkDataset | null => {
        const query = benchmarkQueries[i]
        if (!query?.data) return null
        return {
          key,
          name: query.data.benchmark_name,
          color: benchmarkColor(key, availableBenchmarks),
          data: query.data.data,
          // 0 from a backend older than 2026-09-07.
          anchorUnpriced: query.data.anchor_unpriced_holdings ?? 0,
        }
      })
      .filter((d): d is BenchmarkDataset => d !== null)
  }, [selectedBenchmarks, benchmarkQueries, availableBenchmarks])

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

  // The header line's value change and period gain over the selected range. Extracted
  // to `periodChange` and unit-tested: inline it read the raw endpoints of a series every
  // other consumer trims, and published `0%` for two percentages that were undefined —
  // see its docstring. Both percentages are `number | null` now and the two spans below
  // render nothing for null.
  const performanceMetrics = useMemo(
    () => (valueOverTime ? periodChange(valueOverTime, attribution?.total_pnl_eur) : null),
    [valueOverTime, attribution],
  )

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

    // 4. Win Rate. Extracted and unit-tested for the same reason as the two
    // concentration figures below: it counted every holding the backend could not value
    // as a *losing* position, since an unpriced holding is valued at 0.00 and so carries
    // a gain of -cost. Excluded from both sides now, with the count reported.
    const wins = winRate(positions)

    // 5. Calmar Ratio (XIRR / |Max Drawdown|) — needs an ANNUALIZED numerator,
    // so it goes blank on short ranges where only a period return exists.
    const calmarRatio =
      xirr !== null && xirrMethod === 'xirr' && maxDrawdown !== null && maxDrawdown < 0
        ? xirr / Math.abs(maxDrawdown)
        : null

    // 6. Top 5 Concentration, footnoted with the effective holdings it cannot express:
    // a top-5 weight reads the same for five equal positions and one dominant one.
    const top5Weight = concentrationPct(positions, 5)
    const { effectiveHoldings } = herfindahlConcentration(positions)

    return {
      xirr,
      xirrMethod,
      // Completeness of the valuation the return was measured against. Reported rather
      // than corrected: the tax-lot purchases are unconditional flows, so excluding the
      // security would leave a cost with no matching value.
      xirrUnpriced: annualizedReturn?.unpriced_holdings ?? 0,
      maxDrawdown,
      sharpeRatio: sharpe,
      winRate: wins.pct,
      profitablePositions: wins.profitable,
      totalPositions: wins.judged,
      unvaluedPositions: wins.excluded,
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

    const sortino = sortinoDetail(valueOverTime)

    return {
      volatilityPct: annualizedVolatilityPct(valueOverTime),
      sortino: sortino.ratio,
      sortinoAbsence: sortino.absence,
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
    // The Tabs element is the page, so the section strip can sit directly under the header
    // at full width while the panels stay in the centred column below it.
    <Tabs defaultValue="performance" className="flex min-h-screen flex-col bg-background">
      {/* Header — shared with the crypto view; this view fills its three slots. */}
      <AppHeader
        status={
          <>
            {/* When the data was last refreshed. A skipped run found nothing new, so it
                counts as fresh; the failure case is spelled out just below. */}
            {schedulerStatus?.last_sync && (
              <p className="text-xs text-muted-foreground">
                Updated {formatShortDateTime(schedulerStatus.last_sync.timestamp)}
              </p>
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
          </>
        }
        actions={
          <>
            {/* The label is hidden below `sm`, not the button, and not moved into an
                overflow menu — that would be a second rendering of a four-control
                set. The icon plus this name carry the same meaning to a screen
                reader as the visible label does. */}
            <Button
              onClick={handleSync}
              disabled={syncMutation.isPending}
              className="h-9 px-3 sm:px-4"
              aria-label={syncMutation.isPending ? 'Syncing IBKR data' : 'Sync IBKR data'}
            >
              {syncMutation.isPending ? (
                <RefreshCw className="h-4 w-4 animate-spin sm:mr-2" />
              ) : (
                <RefreshCw className="h-4 w-4 sm:mr-2" />
              )}
              <span className="hidden sm:inline">
                {syncMutation.isPending ? 'Syncing…' : 'Sync'}
              </span>
            </Button>
          </>
        }
        below={
          <>
            {/* Sync status messages — see SyncStatusMessage for why the skip state is
                neutral rather than red. */}
            <SyncStatusMessage
              data={syncMutation.isSuccess ? syncMutation.data : undefined}
              error={syncMutation.isError ? syncMutation.error : null}
              isPending={syncMutation.isPending}
              onForce={() => syncMutation.mutate(true)}
            />
          </>
        }
      />

      {/* Sticky at every width: it is the one control used constantly, and a 3,000px panel
          scrolls under it. On a phone the strip scrolls sideways and the half-cut next
          label is the "there is more" cue. `justify-start` matters: a centred flex row
          wider than its scroller overflows on both sides, and the leading overflow is
          unreachable. */}
      <div className="sticky top-0 z-30 border-b border-border/80 bg-card/90 backdrop-blur supports-[backdrop-filter]:bg-card/80">
        <TabsList
          label="Portfolio sections"
          className="mx-auto w-full max-w-[1400px] justify-start overflow-x-auto px-4 scroll-px-4 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden sm:px-6"
        >
          {/* Most-used first, left to right — the owner's order (2026-09-13). */}
          <TabsTrigger value="performance">Performance</TabsTrigger>
          <TabsTrigger value="dividends">Dividends</TabsTrigger>
          <TabsTrigger value="lookthrough">Look-through</TabsTrigger>
          <TabsTrigger value="allocation">Allocation</TabsTrigger>
          <TabsTrigger value="activity">Activity</TabsTrigger>
          <TabsTrigger value="fundamentals">Fundamentals</TabsTrigger>
          <TabsTrigger value="watchlist">Watchlist</TabsTrigger>
          <TabsTrigger value="tax">Tax</TabsTrigger>
          <TabsTrigger value="forecast">Forecast</TabsTrigger>
        </TabsList>
      </div>

      {/* Constrained and centred: at 1440px and wider a full-bleed grid reads as a
          spreadsheet, and every reference app constrains its content. */}
      <div className="mx-auto w-full max-w-[1400px] flex-1 px-4 pb-10 pt-4 sm:px-6 sm:pt-6">
          {/* Performance Tab */}          {/* Performance Tab */}
          <TabsContent value="performance" className="mt-0 space-y-6">
            {/* The headline, the range's gain, the chart and what the headline is made of. */}
            <PortfolioHero
              summary={summary}
              isLoading={summaryLoading}
              isError={summaryError}
              period={performanceMetrics}
              range={selectedRange}
              controls={
                <div className="flex min-w-0 items-center gap-2">
                  {/* `min-w-0` is load-bearing: without it this flex child refuses to
                      shrink below its content width and overflows the card. All nine
                      ranges stay reachable by scrolling sideways on a phone. */}
                  <SegmentedControl
                    label="Time range"
                    variant="buttons"
                    value={selectedRange}
                    onChange={setSelectedRange}
                    options={TIME_RANGES.map((r) => ({ value: r, label: r }))}
                    className="min-w-0 flex-1 gap-0.5 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden sm:flex-none"
                    buttonClassName="shrink-0"
                  />
                  <BenchmarkPicker
                    selected={selectedBenchmarks}
                    onChange={handleBenchmarkChange}
                  />
                </div>
              }
            >
              <PortfolioValueChart
                data={valueOverTime || []}
                benchmarks={benchmarkDatasets}
                isLoading={chartLoading}
                isError={chartError}
              />
            </PortfolioHero>

            {/* Return and risk, over the selected range. */}
            <PerformanceMetricsCards
              metrics={kpiMetrics}
              isLoading={chartLoading || positionsLoading || xirrLoading}
              isError={chartError || positionsError}
            />
            <RiskMetricsCards
              metrics={riskMetrics}
              dividend={dividendBreakdown?.forward_yield}
              dividendError={dividendBreakdownError}
              isLoading={chartLoading || positionsLoading}
              isError={chartError || positionsError}
            />

            {/* Monthly Returns Heatmap. `rangeStart` is what lets it badge a year or month
                the selected range only partly covers — on 1Y, last year's "YTD" is
                measured from today's date, not from January. */}
            <MonthlyReturnsHeatmap
              data={valueOverTime}
              rangeStart={dateRange.start}
              isLoading={chartLoading}
              isError={chartError}
            />

            {/* Money in per month, with capital deployed beside it */}
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
              // Weight is a share of the account, and cash is part of one — see the
              // prop's docstring for what dividing by holdings alone did to it.
              cash={summary ? { amount: summary.total_cash_eur ?? 0, cash_source: summary.cash_source } : undefined}
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
      </div>

      <AppFooter />
    </Tabs>
  )
}
