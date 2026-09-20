import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { api } from '@/lib/api'
import type { DividendSecurityRow } from '@/lib/api'
import { useFormatCurrency } from '@/lib/CurrencyContext'
import {
  buildChartSeries, dividendMonthLabel as monthLabel, duplicatedSymbols, FC, legendEntries, OTHER,
} from '@/lib/dividendChart'
import { dividendColor, hasIdentity } from '@/lib/dividendColors'
import { dividendPace } from '@/lib/dividendPace'
import { DeltaChip } from './DeltaChip'
import { DividendCalendar } from './DividendCalendar'
import { DividendGrowthPace } from './DividendGrowthPace'
import { DividendKpiCards } from './DividendKpiCards'
import { DividendYearComparison } from './DividendYearComparison'
import { DIVIDEND_CHART_BOX, DividendStackChart } from './DividendStackChart'
import { DividendTtmChart } from './DividendTtmChart'
import { DividendWithholdingControl } from './DividendWithholdingControl'
import { cn } from '@/lib/utils'
import { formatDividendWithholdingPct } from '@/lib/dividendWithholding'
import { DataTable, type Column } from '@/components/ui/DataTable'

function SourceBadge({ row }: { row: DividendSecurityRow }) {
  if (row.payouts === 0 && row.forecast_payouts > 0) {
    return (
      <span
        className="rounded-full bg-violet-100 px-2 py-0.5 text-xs font-medium text-violet-800 dark:bg-violet-950/60 dark:text-violet-200"
        title="No payment received in this period yet — projected from the payout cadence"
      >
        Forecast
      </span>
    )
  }
  if (row.source === 'ibkr') {
    return (
      <span
        className="rounded-full bg-teal-100 px-2 py-0.5 text-xs font-medium text-teal-800 dark:bg-teal-950/60 dark:text-teal-200"
        title="Actual figures from IBKR cash transactions"
      >
        IBKR
      </span>
    )
  }
  return (
    <span
      className="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-950/60 dark:text-amber-200"
      title={
        row.source === 'mixed'
          ? 'Mix of IBKR actuals and Yahoo Finance estimates'
          : 'Estimated gross from Yahoo Finance — withholding not reflected'
      }
    >
      {row.source === 'mixed' ? 'Mixed' : 'Est.'}
    </span>
  )
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
}): Column<DividendSecurityRow>[] {
  const { formatCurrency, duplicated, showForecast } = deps
  return [
    {
      key: 'symbol',
      header: 'Symbol',
      shortHeader: 'Symbol',
      mobile: 'title',
      cellClassName: 'font-medium',
      cell: (row) => (
        <>
          {row.symbol}
          {duplicated.has(row.symbol) && row.exchange && (
            <span className="ml-1 text-xs font-normal text-muted-foreground">{row.exchange}</span>
          )}
        </>
      ),
    },
    {
      key: 'description',
      header: 'Description',
      shortHeader: 'Description',
      mobile: 'meta',
      cellClassName: 'max-w-[14rem] truncate text-muted-foreground',
      cell: (row) => row.description,
    },
    {
      key: 'source',
      header: 'Source',
      shortHeader: 'Source',
      mobile: 'badge',
      cell: (row) => <SourceBadge row={row} />,
    },
    {
      key: 'net',
      header: 'Net',
      shortHeader: 'Net',
      align: 'right',
      mobile: 'value',
      cell: (row) => formatCurrency(row.net_eur),
    },
    {
      key: 'yield',
      header: 'Yield',
      shortHeader: 'Yield',
      align: 'right',
      mobile: 'delta',
      hint: {
        description: "Trailing 12-month net over the position's current market value.",
      },
      cell: (row, view) =>
        row.trailing_yield_pct != null ? (
          <>
            {row.trailing_yield_pct.toFixed(2)}%
            {row.trailing_yield_partial && <span className="ml-0.5 text-muted-foreground">†</span>}
          </>
        ) : view === 'table' ? (
          '—'
        ) : null,
    },
    {
      key: 'fwdYield',
      header: 'Fwd yield',
      shortHeader: 'Fwd yield',
      align: 'right',
      mobile: 'delta',
      hint: {
        description:
          "Projected next-12-month income over the position's current market value. " +
          'Weight these by market value and they average to the portfolio yield on the ' +
          'Performance tab — this column is that figure’s audit. Unlike Forecast beside ' +
          'it, this always covers the next twelve months rather than the selected year, ' +
          'so a row can show no forecast here and still carry a yield.',
      },
      cell: (row, view) =>
        row.forward_yield_pct != null
          ? `${row.forward_yield_pct.toFixed(2)}%`
          : view === 'table'
            ? '—'
            : null,
    },
    {
      key: 'payouts',
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
      header: 'Share',
      shortHeader: 'Share',
      align: 'right',
      cellClassName: 'text-muted-foreground',
      hint: { description: "Share of this period's dividend income, including projections." },
      cell: (row) => (row.share_pct != null ? `${row.share_pct.toFixed(1)}%` : '—'),
    },
    {
      key: 'forecast',
      header: 'Forecast',
      shortHeader: 'Forecast',
      align: 'right',
      cellClassName: 'text-muted-foreground',
      cell: (row) =>
        showForecast && row.forecast_net_eur > 0 ? (
          <>
            +{formatCurrency(row.forecast_net_eur)}
            {row.forecast_basis === 'gross_estimate' && (
              <span className="ml-0.5 text-amber-600 dark:text-amber-500">*</span>
            )}
            {row.forecast_samples != null && row.forecast_samples <= 2 && (
              <span className="ml-0.5 text-amber-600 dark:text-amber-500">
                n={row.forecast_samples}
              </span>
            )}
          </>
        ) : (
          '—'
        ),
    },
    {
      key: 'next',
      header: 'Next',
      shortHeader: 'Next',
      align: 'right',
      cellClassName: 'text-muted-foreground',
      cell: (row) =>
        showForecast && row.next_pay_date ? monthLabel(row.next_pay_date.slice(0, 7), true) : '—',
    },
    {
      key: 'yoc',
      header: 'YoC',
      shortHeader: 'Yield on cost',
      align: 'right',
      cellClassName: 'text-muted-foreground',
      hint: {
        description:
          'Yield on cost: the projected next-12-month income over what the position cost — ' +
          'higher than Fwd yield on a holding that has appreciated, and the same figure the ' +
          'Performance tab shows for the whole portfolio. It uses the projection rather than ' +
          'income already received because those describe different positions once you add to ' +
          'a holding or sell and rebuy it: the income was earned on shares you no longer hold ' +
          'at that cost.',
      },
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
  const formatCurrency = useFormatCurrency()

  const { data, isLoading, isError } = useQuery({
    queryKey: ['dividends', 'breakdown', year],
    // Forecast is always fetched; the toggle only hides it, so flipping is instant.
    queryFn: () => api.getDividendBreakdown(
      typeof year === 'number' ? year : undefined,
      year === '24m' ? '24m' : undefined,
    ),
    staleTime: 30 * 60 * 1000,
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
  const pace = useMemo(
    () => dividendPace(ttmPoints, data?.ttm_coverage_start),
    [ttmPoints, data],
  )

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <CardTitle>Dividends</CardTitle>
            <CardDescription>
              {data && hasAnything ? (
                <>
                  Received {formatCurrency(data.total_net_eur)} net
                  {year === '24m' && data.months.length > 0 && (
                    <> · {monthLabel(data.months[0].month, true)} – {monthLabel(data.months[data.months.length - 1].month, true)}</>
                  )}
                  {showForecast && data.total_forecast_net_eur > 0 && (
                    <> · projected +{formatCurrency(data.total_forecast_net_eur)}</>
                  )}
                  {data.ibkr_from && (
                    <span
                      className="ml-2 text-xs"
                      title={`IBKR actuals from ${data.ibkr_from}; earlier months are Yahoo Finance estimates`}
                    >
                      (IBKR actuals from {data.ibkr_from})
                    </span>
                  )}
                </>
              ) : (
                'Net dividend income by month and stock'
              )}
            </CardDescription>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <div className="inline-flex rounded-md border border-input p-0.5" role="group" aria-label="Dividend chart view">
              {(['monthly', 'ttm'] as const).map(mode => (
                <button
                  key={mode}
                  type="button"
                  aria-pressed={chartMode === mode}
                  onClick={() => setChartMode(mode)}
                  className={cn(
                    'h-8 rounded px-3 text-sm font-medium transition-colors',
                    chartMode === mode ? 'bg-primary text-primary-foreground' : 'hover:bg-accent',
                  )}
                >
                  {mode === 'monthly' ? 'Monthly' : 'TTM'}
                </button>
              ))}
            </div>
            <label htmlFor="dividend-year" className="sr-only">
              Dividend period
            </label>
            <select
              id="dividend-year"
              value={year}
              onChange={(e) => setYear(
                e.target.value === 'all' || e.target.value === '24m' ? e.target.value : Number(e.target.value),
              )}
              className="h-9 rounded-md border border-input bg-background px-3 text-sm font-medium"
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
                  ? 'border-transparent bg-primary text-primary-foreground'
                  : 'border-input bg-background hover:bg-accent hover:text-accent-foreground'
              )}
              title="Overlay projected payments inferred from each stock's payout cadence"
              aria-label="Toggle forecast overlay"
              aria-pressed={showForecast}
            >
              Forecast
            </button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-6">
        {isError ? (
          <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">
            Couldn't load dividends — the backend didn't respond. Try again in a moment.
          </div>
        ) : (
          <>
            {(isLoading || hasGrowth) && (
              <DividendKpiCards
                growth={data?.growth}
                ibkrFrom={data?.ibkr_from ?? null}
                isLoading={isLoading}
              />
            )}

            {/* Outside the chart branch below, so it survives an empty selected
                year: the pace is measured over the whole history, and a year with
                no income is exactly when the trend is worth knowing. */}
            <DividendGrowthPace pace={pace} />

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
                <div className="-mx-1 flex flex-wrap items-center gap-x-1 gap-y-0.5 text-xs text-muted-foreground">
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
                          'inline-flex items-center gap-1.5 rounded px-1 py-0.5 transition-opacity hover:bg-accent',
                          dimmed && 'opacity-40',
                          !isOther && pinnedVisible === symbol && 'bg-accent font-medium text-foreground',
                        )}
                      >
                        <span
                          className="h-2.5 w-2.5 shrink-0 rounded-sm"
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
                        translucent = forecast
                      </span>
                      {securities.some((r) => r.forecast_basis === 'gross_estimate') && (
                        <span>
                          <span className="text-amber-600 dark:text-amber-500">*</span> estimated net
                          from gross dividends using {formatDividendWithholdingPct(
                            data.forecast_withholding_pct,
                          )}% assumed withholding;
                          actual net payments may differ
                        </span>
                      )}
                      {securities.some(
                        (r) => r.forecast_net_eur > 0 && (r.forecast_samples ?? 9) <= 2
                      ) && (
                        <span>
                          <span className="text-amber-600 dark:text-amber-500">n=2</span> cadence
                          inferred from one or two payments — a guess, not an observed pattern
                        </span>
                      )}
                    </>
                  )}
                  {securities.some((r) => r.trailing_yield_partial && r.trailing_yield_pct != null) && (
                    <span>
                      <span className="text-muted-foreground">†</span> held less than the full
                      year, so partial income over a full position value reads low — deliberately
                      not annualized
                    </span>
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
                    withholdingPct={data.forecast_withholding_pct}
                  />
                </div>

                <DataTable
                  rows={securities}
                  columns={columns}
                  getRowKey={(row) => row.security_id}
                  label="Per-position dividend table"
                  density="compact"
                  className="border-t border-border pt-5"
                />
              </>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}
