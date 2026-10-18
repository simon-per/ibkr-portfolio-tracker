import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChartColumnStacked, HandCoins, ListOrdered } from 'lucide-react'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { SectionHeader } from '@/components/ui/SectionHeader'
import { api } from '@/lib/api'
import type { DividendSecurityRow } from '@/lib/api'
import { useFormatCurrency, useKeepPreviousData } from '@/lib/CurrencyContext'
import {
  buildChartSeries, dividendMonthLabel as monthLabel, duplicatedSymbols, FC, legendEntries, OTHER,
} from '@/lib/dividendChart'
import { dividendColor, hasIdentity } from '@/lib/dividendColors'
import { forecastTrend, type LiveForecast } from '@/lib/dividendForecastTrend'
import { deltaColor } from '@/lib/delta'
import { payoutChanges, RAISE_BAND_PCT } from '@/lib/dividendGrowth'
import {
  DEFAULT_DIVIDEND_SORT, initialDirection, sortDividendRows, type DividendSortKey,
} from '@/lib/dividendTable'
import { dividendPace } from '@/lib/dividendPace'
import { DeltaChip } from './DeltaChip'
import { DividendCalendar } from './DividendCalendar'
import { DividendForecastTrend } from './DividendForecastTrend'
import { DividendGrowthPace } from './DividendGrowthPace'
import { DividendKpiCards, type DividendKpiTrend } from './DividendKpiCards'
import { DividendSubheading } from './DividendSubheading'
import { DividendYearComparison } from './DividendYearComparison'
import { DIVIDEND_CHART_BOX, DividendStackChart } from './DividendStackChart'
import { DividendTtmChart } from './DividendTtmChart'
import { DividendWithholdingControl } from './DividendWithholdingControl'
import { cn, placeholderDim } from '@/lib/utils'
import { DataTable, type Column } from '@/components/ui/DataTable'
import { SegmentedControl } from '@/components/ui/SegmentedControl'
import { displayName } from '@/lib/securityName'
import { TickerAvatar } from '@/components/ui/TickerAvatar'

/**
 * The one provenance mark the owner keeps (2026-10-14): `est.` on a row whose income is
 * projected or estimated rather than paid by the broker. Which source it came from is not
 * the reader's business.
 */
function isEstimated(row: DividendSecurityRow): boolean {
  return (row.payouts === 0 && row.forecast_payouts > 0) || (row.source !== 'ibkr' && row.source !== 'mixed')
}

/**
 * The per-position dividend columns, described once for both renderings.
 *
 * Two of these exist only on the phone. Gross and withholding were previously reachable
 * only through the Net cell's `title=` — a hover, so on a touch device those figures
 * did not exist at all. As `desktop: 'hide'` detail columns they are on the card and
 * still out of the desktop table's ten.
 */
function dividendColumns(deps: {
  formatCurrency: (v: number) => string
  duplicated: Set<string>
  showForecast: boolean
}): Column<DividendSecurityRow, DividendSortKey>[] {
  const { formatCurrency, duplicated, showForecast } = deps
  return [
    {
      key: 'symbol',
      sortKey: 'symbol',
      header: 'Position',
      shortHeader: 'Symbol',
      mobile: 'title',
      cell: (row, view) => {
        const exchange = duplicated.has(row.symbol) && row.exchange ? row.exchange : null
        const est = isEstimated(row) ? (
          <span className="ml-1.5 text-xs font-normal text-muted-foreground">est.</span>
        ) : null
        return view === 'table' ? (
          <div className="flex items-center gap-3">
            <TickerAvatar symbol={row.symbol} securityId={row.security_id} className="h-8 w-8 text-[10px]" />
            <div className="min-w-0">
              <div className="max-w-[15rem] truncate font-medium" title={row.description}>
                {displayName(row.description) || row.symbol}
                {est}
              </div>
              <div className="text-xs text-muted-foreground">
                {row.symbol}
                {exchange && ` · ${exchange}`}
              </div>
            </div>
          </div>
        ) : (
          <>
            {row.symbol}
            {exchange && <span className="ml-1 text-xs font-normal text-muted-foreground">{exchange}</span>}
            {est}
          </>
        )
      },
    },
    {
      key: 'description',
      header: 'Name',
      shortHeader: 'Name',
      mobile: 'meta',
      desktop: 'hide',
      cell: (row) => displayName(row.description),
    },
    {
      key: 'net',
      sortKey: 'net',
      header: 'Net',
      shortHeader: 'Net',
      align: 'right',
      mobile: 'value',
      cell: (row) => formatCurrency(row.net_eur),
    },
    {
      key: 'yield',
      sortKey: 'yield',
      header: 'Yield',
      shortHeader: 'Yield',
      align: 'right',
      mobile: 'delta',
      hint: { description: 'Trailing 12M net ÷ market value' },
      cell: (row, view) =>
        row.trailing_yield_pct != null ? (
          `${row.trailing_yield_pct.toFixed(2)}%`
        ) : view === 'table' ? (
          '—'
        ) : null,
    },
    {
      key: 'fwdYield',
      sortKey: 'fwdYield',
      header: 'Fwd yield',
      shortHeader: 'Fwd yield',
      align: 'right',
      mobile: 'delta',
      hint: { description: 'Next 12M forecast ÷ market value' },
      cell: (row, view) =>
        row.forward_yield_pct != null
          ? `${row.forward_yield_pct.toFixed(2)}%`
          : view === 'table'
            ? '—'
            : null,
    },
    {
      key: 'payouts',
      sortKey: 'payouts',
      header: 'Payouts',
      shortHeader: 'Payouts',
      align: 'right',
      cell: (row) => (
        <>
          {row.payouts}
          {showForecast && row.forecast_payouts > 0 && (
            <span className="text-muted-foreground"> +{row.forecast_payouts}</span>
          )}
        </>
      ),
    },
    {
      key: 'share',
      sortKey: 'share',
      header: 'Share',
      shortHeader: 'Share',
      align: 'right',
      cellClassName: 'text-muted-foreground',
      hint: { description: "Share of this period's income" },
      cell: (row) => (row.share_pct != null ? `${row.share_pct.toFixed(1)}%` : '—'),
    },
    {
      key: 'forecast',
      sortKey: 'forecast',
      header: 'Forecast',
      shortHeader: 'Forecast',
      align: 'right',
      cellClassName: 'text-muted-foreground',
      cell: (row) =>
        showForecast && row.forecast_net_eur > 0 ? (
          `+${formatCurrency(row.forecast_net_eur)}`
        ) : (
          '—'
        ),
    },
    {
      key: 'next',
      sortKey: 'next',
      header: 'Next',
      shortHeader: 'Next',
      align: 'right',
      cellClassName: 'text-muted-foreground',
      cell: (row) =>
        showForecast && row.next_pay_date ? monthLabel(row.next_pay_date.slice(0, 7), true) : '—',
    },
    {
      key: 'growth',
      sortKey: 'growth',
      header: 'Growth',
      shortHeader: 'Payout growth',
      align: 'right',
      // Toned by the same flat band the "raised · cut" count above the table uses, so a
      // green row and that count cannot disagree about what a raise is.
      tone: (row) =>
        row.per_share_growth_pct != null
          ? deltaColor(row.per_share_growth_pct, RAISE_BAND_PCT)
          : 'text-muted-foreground',
      hint: { description: 'Payout per share vs a year earlier' },
      cell: (row) =>
        row.per_share_growth_pct != null
          ? `${row.per_share_growth_pct > 0 ? '+' : ''}${row.per_share_growth_pct.toFixed(1)}%`
          : '—',
    },
    {
      key: 'yoc',
      sortKey: 'yoc',
      header: 'YoC',
      shortHeader: 'Yield on cost',
      align: 'right',
      cellClassName: 'text-muted-foreground',
      hint: { description: 'Next 12M forecast ÷ cost' },
      cell: (row) =>
        row.yield_on_cost_pct != null ? `${row.yield_on_cost_pct.toFixed(2)}%` : '—',
    },
    {
      key: 'gross',
      header: 'Gross',
      shortHeader: 'Gross',
      align: 'right',
      desktop: 'hide',
      cell: (row) => formatCurrency(row.gross_eur),
    },
    {
      key: 'withholding',
      header: 'Withholding',
      shortHeader: 'Withholding',
      align: 'right',
      desktop: 'hide',
      cell: (row) => formatCurrency(row.withholding_eur),
    },
  ]
}

export function DividendsTab() {
  const now = new Date()
  const currentYear = now.getFullYear()
  const currentMonth = `${currentYear}-${String(now.getMonth() + 1).padStart(2, '0')}`
  const [year, setYear] = useState<number | 'all' | '24m'>(currentYear)
  const [chartMode, setChartMode] = useState<'monthly' | 'ttm'>('monthly')
  const [showForecast, setShowForecast] = useState(true)
  // Hovering or focusing a legend entry highlights its segments; clicking one pins that
  // highlight so it survives the pointer leaving. `pinned` wins, so a click is not undone
  // by the mouse moving away.
  const [hovered, setHovered] = useState<string | null>(null)
  const [pinned, setPinned] = useState<string | null>(null)
  const [otherOpen, setOtherOpen] = useState(false)
  const [sortColumn, setSortColumn] = useState<DividendSortKey>(DEFAULT_DIVIDEND_SORT.column)
  const [sortDirection, setSortDirection] = useState(DEFAULT_DIVIDEND_SORT.direction)
  const handleSort = (column: DividendSortKey) => {
    if (column === sortColumn) {
      setSortDirection((d) => (d === 'asc' ? 'desc' : 'asc'))
    } else {
      setSortColumn(column)
      setSortDirection(initialDirection(column))
    }
  }
  const formatCurrency = useFormatCurrency()
  const keepPrevious = useKeepPreviousData()

  const { data, isLoading, isError, isPlaceholderData } = useQuery({
    queryKey: ['dividends', 'breakdown', year],
    // Forecast is always fetched; the toggle only hides it, so flipping is instant.
    queryFn: () => api.getDividendBreakdown(
      typeof year === 'number' ? year : undefined,
      year === '24m' ? '24m' : undefined,
    ),
    staleTime: 30 * 60 * 1000,
    // The last period stays while the next one loads (#68): the year bars below and the
    // withholding control sit inside the data branch, so blanking it unmounted the bar just
    // pressed and collapsed the page under it.
    placeholderData: keepPrevious,
  })

  // Extracted so it can be unit-tested: this transformation already carried one
  // silent bug (ranking by a key space the data didn't use), and with no browser
  // in the loop a test is the only way to catch the next one — the chart itself
  // is invisible to the component suite, which mocks recharts' container away.
  // Both views come out of one call, over one server-supplied order, so neither the
  // view nor the toggle can move a series.
  const { chartData, ttmData, ttmPoints, stackSymbols } = useMemo(
    () => buildChartSeries(data, { showForecast, currentMonth }),
    [data, showForecast, currentMonth],
  )

  // The colour order is the server's, not this view's — see `lib/dividendColors.ts`.
  // `stackSymbols` is only what is drawn here, and a holding's colour must not depend on it.
  const stackOrder = useMemo(() => data?.stack_order ?? [], [data])
  const colorOf = (sym: string) => dividendColor(sym, stackOrder)

  // Growth per month rides on the response rather than being derived here: it is
  // measured over the whole history, which a year-filtered payload doesn't carry.
  const growthByMonth = useMemo(() => {
    const m = new Map<string, { mom: number | null; yoy: number | null }>()
    for (const bar of data?.months ?? []) {
      m.set(bar.month, { mom: bar.mom_pct ?? null, yoy: bar.yoy_pct ?? null })
    }
    return m
  }, [data])

  // Only on realized months — a projected month's "change" would be an artifact
  // of the forecast's own flat median, and the backend sends null.
  const monthlyTooltipFooter = (month: string) => {
    const growth = growthByMonth.get(month)
    if (!growth || (growth.mom == null && growth.yoy == null)) return null
    return (
      <>
        <DeltaChip pct={growth.mom} label="MoM" />
        <DeltaChip pct={growth.yoy} label="YoY" />
      </>
    )
  }

  const yearOptions = useMemo(() => {
    const ys = data?.years?.length ? [...data.years] : [currentYear]
    return ys
      .filter((candidate) => showForecast || candidate <= currentYear)
      .sort((a, b) => b - a)
  }, [data, currentYear, showForecast])

  const toggleForecast = () => {
    const next = !showForecast
    // A future year exists only to display projections. Keep the select and the
    // chart on the same valid range when those projections are hidden.
    if (!next && typeof year === 'number' && year > currentYear) setYear(currentYear)
    setShowForecast(next)
  }

  const securities = useMemo(() => {
    const rows = data?.securities ?? []
    // Without the forecast the forecast-only rows are pure noise.
    return showForecast ? rows : rows.filter((r) => r.payouts > 0)
  }, [data, showForecast])

  const sortedSecurities = useMemo(
    () => sortDividendRows(securities, sortColumn, sortDirection, showForecast),
    [securities, sortColumn, sortDirection, showForecast],
  )
  // Counted over the rows the table shows, so the line can be checked against its
  // Growth column.
  const changes = useMemo(() => payoutChanges(securities), [securities])

  // Today's figures for the forecast card, so it and the KPI tile print one
  // "Next 12 months" (lib/dividendForecastTrend.ts, rule 3).
  const live = useMemo<LiveForecast | null>(
    () => (data?.growth
      ? { next12m: data.growth.next_12m_eur, ttm: data.growth.ttm.net_eur, currency: data.base_currency }
      : null),
    [data],
  )

  // The KPI sparklines: the forecast history's two series, ending on `live`. Same query key
  // as the forecast card, so the two share one request and one cache entry.
  const { data: forecastHistory } = useQuery({
    queryKey: ['dividends', 'forecast-history'],
    queryFn: () => api.getDividendForecastHistory(),
  })
  const kpiTrend = useMemo<DividendKpiTrend | null>(() => {
    const trend = forecastTrend(forecastHistory, live)
    if (!trend) return null
    return {
      received: trend.points.map((p) => p.received),
      forecast: trend.points.map((p) => p.value),
    }
  }, [forecastHistory, live])

  // A ticker listed on two venues is two securities and two rows; only the table
  // needs the venue, and only where it actually disambiguates.
  const duplicated = useMemo(() => duplicatedSymbols(securities), [securities])

  const columns = useMemo(
    () => dividendColumns({ formatCurrency, duplicated, showForecast }),
    [formatCurrency, duplicated, showForecast]
  )

  const upcoming = useMemo(
    () => (showForecast ? data?.upcoming ?? [] : []),
    [data, showForecast],
  )

  const hasAnything = (data?.total_net_eur ?? 0) > 0 || (showForecast && (data?.total_forecast_net_eur ?? 0) > 0)

  // Per view, not per response. `total_forecast_net_eur` covers the SELECTED
  // window, while a rolling window reaches twelve months past it — so a year view
  // can draw dashed segments while that total is 0, leaving hatched bars on screen
  // with nothing saying what the hatching means.
  const visibleRows = chartMode === 'ttm' ? ttmData : chartData
  const chartHasForecast = visibleRows.some((row) =>
    Object.entries(row).some(([k, v]) => k.startsWith(FC) && typeof v === 'number' && v > 0),
  )
  // Only what is actually on screen: a swatch for a bucket with no segment behind it is a
  // legend entry for nothing.
  const legend = useMemo(
    () => legendEntries(visibleRows, stackSymbols),
    [visibleRows, stackSymbols],
  )
  // A pin only counts while the holding it names is on screen. Switching to a range that
  // does not contain it would otherwise dim every series against a symbol nobody can see,
  // with no visible control to undo it; the pin is kept, so going back restores it.
  const pinnedVisible = pinned != null && legend.some((e) => e.symbol === pinned) ? pinned : null
  const activeSymbol = pinnedVisible ?? hovered
  // What the fold contains, from the same per-security rows the table below renders, so the
  // two cannot disagree about a holding's figure for this range.
  const otherMembers = useMemo(
    () => securities
      .filter((r) => !hasIdentity(r.symbol, stackOrder))
      .map((r) => ({
        symbol: r.symbol,
        total: r.net_eur + (showForecast ? r.forecast_net_eur : 0),
      }))
      .filter((r) => r.total > 0)
      .sort((a, b) => b.total - a.total),
    [securities, stackOrder, showForecast],
  )
  // The growth block is unwindowed, so it stands even when the selected year is
  // empty — which is exactly when knowing the trend is most useful.
  const hasGrowth = (data?.growth?.annual?.length ?? 0) > 0

  // Measured across `ttmPoints`, which is already the range slice with the open
  // windows dropped when Forecast is off — so the rate answers the range the
  // reader selected, and "which windows are showing" is stated once rather than
  // recomputed here or restated on the server.
  const pace = useMemo(() => dividendPace(ttmPoints), [ttmPoints])

  return (
    <Card>
      {/* The title half is SectionHeader's; the controls are a wrapping row of their own, not
          its `aside` — four controls are wider than a phone, and the aside does not shrink. */}
      <CardHeader className="flex-row flex-wrap items-center justify-between gap-x-4 gap-y-3 space-y-0">
      <SectionHeader
        variant="bare"
        icon={HandCoins}
        title="Dividends"
        description={
              data && hasAnything ? (
                <span className={placeholderDim(isPlaceholderData)}>
                  Received {formatCurrency(data.total_net_eur)} net
                  {year === '24m' && data.months.length > 0 && (
                    <> · {monthLabel(data.months[0].month, true)} – {monthLabel(data.months[data.months.length - 1].month, true)}</>
                  )}
                  {showForecast && data.total_forecast_net_eur > 0 && (
                    <> · projected +{formatCurrency(data.total_forecast_net_eur)}</>
                  )}
                </span>
              ) : (
                'Net dividend income by month and stock'
              )
        }
      />
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <SegmentedControl
              label="Dividend chart view"
              value={chartMode}
              onChange={setChartMode}
              options={[
                { value: 'monthly', label: 'Monthly' },
                { value: 'ttm', label: 'TTM' },
              ]}
            />
            <label htmlFor="dividend-year" className="sr-only">
              Dividend period
            </label>
            <select
              id="dividend-year"
              value={year}
              onChange={(e) => setYear(
                e.target.value === 'all' || e.target.value === '24m' ? e.target.value : Number(e.target.value),
              )}
              className="h-9 rounded-lg border border-input bg-card px-3 text-sm font-medium"
            >
              <option value="all">All time</option>
              <option value="24m">Last 24 months</option>
              {yearOptions.map((y) => (
                <option key={y} value={y}>
                  {y}
                </option>
              ))}
            </select>
            {data && (
              <DividendWithholdingControl appliedPct={data.forecast_withholding_pct} />
            )}
            <button
              onClick={toggleForecast}
              className={cn(
                'inline-flex h-9 items-center rounded-md border px-3 text-sm font-medium transition-colors',
                showForecast
                  ? 'border-primary/30 bg-primary/10 text-primary'
                  : 'border-border bg-card text-muted-foreground hover:bg-muted hover:text-foreground'
              )}
              title="Show projected payments"
              aria-label="Toggle forecast overlay"
              aria-pressed={showForecast}
            >
              Forecast
            </button>
          </div>
      </CardHeader>
      <CardContent
        className={cn('space-y-6', placeholderDim(isPlaceholderData))}
        aria-busy={isPlaceholderData || undefined}
      >
        {isError ? (
          <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">
            Couldn't load dividends — the server didn't respond. Try again in a moment.
          </div>
        ) : (
          <>
            {(isLoading || hasGrowth) && (
              <DividendKpiCards
                growth={data?.growth}
                isLoading={isLoading}
                trend={kpiTrend}
              />
            )}

            {/* Outside the chart branch below, so it survives an empty selected
                year: the pace is measured over the whole history, and a year with
                no income is exactly when the trend is worth knowing. */}
            <DividendGrowthPace pace={pace} />

            {/* The forecast's own history. A projection, so it goes with the toggle. */}
            {showForecast && <DividendForecastTrend live={live} />}

            {isLoading ? (
              <div className={cn(DIVIDEND_CHART_BOX, 'animate-pulse rounded bg-muted')} />
            ) : !data || (!hasAnything && chartMode === 'monthly') ? (
              <div className="flex h-32 items-center justify-center text-center text-sm text-muted-foreground">
                No dividends recorded {year === 'all' ? 'yet' : year === '24m' ? 'in the last 24 months' : `for ${year}`}. They arrive with
                the IBKR sync; estimates for earlier years come from the dividend sync.
              </div>
            ) : (
              <>
                {chartMode === 'ttm' ? (
                  <DividendTtmChart
                    data={ttmData}
                    points={ttmPoints}
                    stackSymbols={stackSymbols}
                    colorOf={colorOf}
                    showForecast={showForecast}
                    multiYear={typeof year !== 'number'}
                    activeSymbol={activeSymbol}
                  />
                ) : (
                  <section aria-label="Monthly dividends" className="space-y-3">
                  <DividendSubheading title="Monthly by holding" icon={ChartColumnStacked} />
                  <DividendStackChart
                    data={chartData}
                    stackSymbols={stackSymbols}
                    colorOf={colorOf}
                    showForecast={showForecast}
                    multiYear={typeof year !== 'number'}
                    tooltipTitle={(m) => monthLabel(m, true)}
                    tooltipFooter={monthlyTooltipFooter}
                    activeSymbol={activeSymbol}
                  />
                  </section>
                )}

                {/* The three qualifier explanations used to live in `title=`, which no
                    touch device can reach — and they are the difference between reading
                    a projection as a measurement and not. The legend already wraps, so
                    they are simply visible now, matching DividendCalendar and
                    DividendYearComparison, which both spell theirs out. */}
                {/* Both views are stacked by the same symbols in the same colours, so the
                    key belongs to both. Ordered by what this range paid — the one thing the
                    chart cannot show — which is free now that colour no longer follows rank.
                    Real buttons, so isolating a holding works from the keyboard and on a
                    touch device, where a hover-only affordance does not exist at all. */}
                <div className="-mx-2 flex flex-wrap items-center gap-x-0.5 gap-y-0.5 text-xs text-muted-foreground">
                  {legend.map(({ symbol, total }) => {
                    const dimmed = activeSymbol != null && activeSymbol !== symbol
                    const isOther = symbol === OTHER
                    return (
                      <button
                        key={symbol}
                        type="button"
                        onMouseEnter={() => setHovered(symbol)}
                        onMouseLeave={() => setHovered(null)}
                        onFocus={() => setHovered(symbol)}
                        onBlur={() => setHovered(null)}
                        onClick={() =>
                          isOther
                            ? setOtherOpen((open) => !open)
                            : setPinned((cur) => (cur === symbol ? null : symbol))
                        }
                        aria-pressed={isOther ? undefined : pinnedVisible === symbol}
                        aria-expanded={isOther ? otherOpen : undefined}
                        title={`${symbol}: ${formatCurrency(total)} in this range`}
                        className={cn(
                          'inline-flex items-center gap-1.5 rounded-full border border-transparent px-2 py-0.5 transition-opacity hover:bg-accent',
                          dimmed && 'opacity-40',
                          !isOther && pinnedVisible === symbol && 'border-border bg-accent font-medium text-foreground',
                        )}
                      >
                        <span
                          className="h-2.5 w-2.5 shrink-0 rounded-full"
                          style={{ backgroundColor: colorOf(symbol) }}
                        />
                        {isOther
                          ? `Other · ${otherMembers.length} holding${otherMembers.length === 1 ? '' : 's'}`
                          : symbol}
                      </button>
                    )
                  })}
                </div>

                {/* What is inside the fold, on the surface rather than in a tooltip. It can
                    be a fifth of the bar, and until this there was no way to look in. */}
                {otherOpen && otherMembers.length > 0 && (
                  <ul
                    aria-label="Holdings folded into Other"
                    className="flex flex-wrap gap-x-4 gap-y-1 rounded-md border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground"
                  >
                    {otherMembers.map((m) => (
                      <li key={m.symbol} className="inline-flex items-center gap-1.5">
                        <span>{m.symbol}</span>
                        <span className="tabular-nums">{formatCurrency(m.total)}</span>
                      </li>
                    ))}
                  </ul>
                )}

                <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-muted-foreground">
                  {showForecast && chartHasForecast && (
                    <>
                      <span className="inline-flex items-center gap-1.5">
                        <span className="h-2.5 w-2.5 rounded-sm border border-dashed border-muted-foreground/70" />
                        Forecast
                      </span>
                    </>
                  )}
                </div>

                {/* Per-year growth beside what is coming: the two questions the
                    monthly chart cannot answer on a quarterly cadence.
                    Two columns only when both halves have something to show —
                    with the forecast off the calendar is empty, and a bare
                    half-width panel beside dead space looks like a load failure. */}
                <div
                  className={cn(
                    'grid gap-6 border-t border-border pt-5 [&>*]:min-w-0',
                    hasGrowth && upcoming.length > 0 && 'lg:grid-cols-2',
                  )}
                >
                  {hasGrowth && (
                    <DividendYearComparison
                      annual={data.growth!.annual}
                      selectedYear={typeof year === 'number' ? year : 'all'}
                      onSelectYear={setYear}
                      showForecast={showForecast}
                    />
                  )}
                  <DividendCalendar
                    upcoming={upcoming}
                    colorOf={colorOf}
                  />
                </div>

                <div className="space-y-3 border-t border-border pt-5">
                  <DividendSubheading
                    title="By position"
                    icon={ListOrdered}
                    aside={changes ? (
                      <p aria-label="Payout changes" className="text-xs tabular-nums text-muted-foreground">
                        <span className="font-medium">Payouts</span>
                        {' · '}
                        <span className={cn(changes.raised > 0 && 'font-medium text-gain')}>{changes.raised} raised</span>
                        {' · '}
                        <span className={cn(changes.cut > 0 && 'font-medium text-loss')}>{changes.cut} cut</span>
                      </p>
                    ) : undefined}
                  />
                  <DataTable
                    rows={sortedSecurities}
                    columns={columns}
                    getRowKey={(row) => row.security_id}
                    label="Per-position dividend table"
                    density="compact"
                    sort={{ column: sortColumn, direction: sortDirection, onSort: handleSort }}
                  />
                </div>
              </>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}
