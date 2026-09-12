import { useState, useMemo, useEffect } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { XAxis, YAxis, CartesianGrid, Legend, Tooltip, ResponsiveContainer, Area, AreaChart } from 'recharts'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { formatCount } from '@/lib/utils'
import { forecastBaseline, forecastSeries, projectForecast } from '@/lib/forecast'
import { useBaseCurrency, useCurrencySymbol } from '@/lib/CurrencyContext'
import { useIsCompact } from '@/lib/useMediaQuery'
import { readStored, writeStored } from '@/lib/storage'
import { DataTable, type Column } from '@/components/ui/DataTable'

const STORAGE_KEYS = {
  monthlyContribution: 'forecast.monthlyContribution',
  expectedReturn: 'forecast.expectedReturn',
  startFromZero: 'forecast.startFromZero',
  forecastYears: 'forecast.forecastYears',
}

function readNumber(key: string, fallback: number, min: number, max: number): number {
  const saved = readStored(key)
  if (!saved) return fallback
  const val = Number(saved)
  if (!isFinite(val) || isNaN(val) || val < min || val > max) return fallback
  return val
}

interface ProjectionRow {
  year: number
  value: number
  moneyIn: number | null
  gains: number | null
}

/** The horizons the table reports — fixed, independent of the chart's slider. */
const HORIZON_YEARS = [1, 5, 10, 15, 20]

const SCENARIOS = [
  { name: 'Conservative', rate: 5 },
  { name: 'Moderate', rate: 8 },
  { name: 'Aggressive', rate: 12 },
]

/**
 * The projection table's four columns, described once for both renderings.
 *
 * "Money In" and "Investment Gains" partition "Portfolio Value" on every row — the
 * baseline is what was paid in, not the market value, and `lib/forecast.ts` says why. The
 * column used to be called "Total Contributions" and meant a *different* quantity from
 * the chart band of the same name: neither included what had already been paid in, and
 * the chart's included every gain ever made. Both render a dash rather than a figure when
 * the baseline could not be loaded, because a 0 would claim nothing was ever paid in.
 */
const projectionColumns = (curSym: string): Column<ProjectionRow>[] => [
  {
    key: 'horizon',
    header: 'Time Horizon',
    shortHeader: 'Horizon',
    mobile: 'title',
    cellClassName: 'font-medium',
    cell: (p) => `${p.year} ${p.year === 1 ? 'Year' : 'Years'}`,
  },
  {
    key: 'value',
    header: 'Portfolio Value',
    shortHeader: 'Portfolio value',
    align: 'right',
    mobile: 'value',
    tone: () => 'text-green-600 dark:text-green-400',
    cellClassName: 'font-semibold',
    cell: (p) => `${curSym}${formatCount(p.value)}`,
  },
  {
    key: 'gains',
    header: 'Investment Gains',
    shortHeader: 'Investment gains',
    align: 'right',
    mobile: 'delta',
    // A book worth less than was paid in is a loss today, and a loss in blue reads as a gain.
    tone: (p) =>
      p.gains !== null && p.gains < 0 ? 'text-red-600 dark:text-red-400' : 'text-blue-600 dark:text-blue-400',
    hint: {
      description:
        'Portfolio Value minus Money In: the gain or loss already made today, plus what the projection adds.',
    },
    cell: (p) => (p.gains === null ? '—' : `${curSym}${formatCount(p.gains)}`),
  },
  {
    key: 'moneyIn',
    header: 'Money In',
    shortHeader: 'Money in',
    align: 'right',
    hint: {
      description:
        'What you have paid in so far plus the monthly contributions projected to this horizon. Money moved between holdings does not count.',
    },
    cell: (p) => (p.moneyIn === null ? '—' : `${curSym}${formatCount(p.moneyIn)}`),
  },
]

export function ForecastTab() {
  const curSym = useCurrencySymbol()
  const isCompact = useIsCompact()
  const { baseCurrency } = useBaseCurrency()
  const [monthlyContribution, setMonthlyContribution] = useState(() => readNumber(STORAGE_KEYS.monthlyContribution, 1000, 0, 1000000))
  const [expectedReturn, setExpectedReturn] = useState(() => readNumber(STORAGE_KEYS.expectedReturn, 8, 0, 30))
  const [startFromZero, setStartFromZero] = useState(() => {
    const saved = readStored(STORAGE_KEYS.startFromZero)
    return saved === 'true'
  })
  const [forecastYears, setForecastYears] = useState(() => readNumber(STORAGE_KEYS.forecastYears, 10, 1, 30))

  // Persist whenever values change, through the one guarded store (`lib/storage.ts`).
  useEffect(() => {
    writeStored(STORAGE_KEYS.monthlyContribution, monthlyContribution.toString())
  }, [monthlyContribution])

  useEffect(() => {
    writeStored(STORAGE_KEYS.expectedReturn, expectedReturn.toString())
  }, [expectedReturn])

  useEffect(() => {
    writeStored(STORAGE_KEYS.startFromZero, startFromZero.toString())
  }, [startFromZero])

  useEffect(() => {
    writeStored(STORAGE_KEYS.forecastYears, forecastYears.toString())
  }, [forecastYears])

  // Two reads, both under the keys Dashboard already fetches with, so neither costs a
  // request once the Performance tab has loaded. The summary query used to carry no
  // `staleTime` and refetched on every visit to this tab.
  const { data: summary, isError: summaryError } = useQuery({
    queryKey: ['portfolio', 'summary'],
    queryFn: () => api.getPortfolioSummary(),
    staleTime: 30 * 60 * 1000,
  })
  const { data: contributions, isPending: contributionsPending } = useQuery({
    queryKey: ['portfolio', 'contributions'],
    queryFn: () => api.getContributions(),
    staleTime: 30 * 60 * 1000,
  })

  // `summary?.total_market_value_eur || 0` on its own made a failed request
  // indistinguishable from an empty portfolio — and "Current" is the *selected* starting
  // point, so the whole projection quietly became contributions-only, understating by
  // the compounded value of the entire book with nothing on screen saying so. A user
  // reads a wrong forecast as their forecast.
  //
  // The figure still falls back to 0 (there is nothing else to project from), but the
  // failure is now stated, and the Current button says so rather than advertising a
  // portfolio value of zero.
  //
  // `current` is what the account is worth today whichever starting point is selected.
  // The button used to print the *selected* seed, so choosing "0" relabelled it
  // "Current (CHF 0)". The projection itself runs from `startValue` / `moneyInToDate`,
  // which `forecastBaseline` resolves — Total Value where cash is tracked, and money in
  // to date from the contributions endpoint, or `null` when that could not be loaded.
  const current = forecastBaseline(summary, contributions, false)
  const { startValue, moneyInToDate } = forecastBaseline(summary, contributions, startFromZero)

  const inputs = useMemo(
    () => ({ startValue, moneyInToDate, monthlyContribution, annualReturnPct: expectedReturn }),
    [startValue, moneyInToDate, monthlyContribution, expectedReturn],
  )

  // One formula for the three surfaces. It was written out four times in this file — the
  // table, the scenarios, the sampled chart series and that series' hand-copied final
  // point — so moving the baseline off market value would have been four edits, and the
  // table and the chart had already drifted into publishing two different quantities
  // under one name. `lib/forecast.ts` is the single copy.
  const projections = useMemo(
    () => HORIZON_YEARS.map((year) => ({ year, ...projectForecast(inputs, year * 12) })),
    [inputs],
  )

  const scenarios = useMemo(
    () =>
      SCENARIOS.map(({ name, rate }) => ({
        name,
        rate,
        value: projectForecast({ ...inputs, annualReturnPct: rate }, forecastYears * 12).value,
      })),
    [inputs, forecastYears],
  )

  // Every 6 months for a cleaner chart; the exact horizon is always the last point.
  const monthlyData = useMemo(() => forecastSeries(inputs, forecastYears * 12), [inputs, forecastYears])

  // Calculate dynamic Y-axis configuration — always produces ≤ 10 ticks
  const yAxisConfig = useMemo(() => {
    const TARGET_TICKS = 8

    if (monthlyData.length === 0) {
      return { domain: [0, 100000] as [number, number], ticks: [0, 50000, 100000] }
    }

    const maxValue = Math.max(...monthlyData.map(d => d.value), 0)

    if (!isFinite(maxValue) || isNaN(maxValue) || maxValue <= 0) {
      return { domain: [0, 100000] as [number, number], ticks: [0, 50000, 100000] }
    }

    // Pick a "nice" interval so we get roughly TARGET_TICKS ticks
    const rawInterval = maxValue / TARGET_TICKS
    const magnitude = Math.pow(10, Math.floor(Math.log10(rawInterval)))
    const normalised = rawInterval / magnitude                // 1–10 range
    const niceStep = normalised <= 1 ? 1 : normalised <= 2 ? 2 : normalised <= 5 ? 5 : 10
    const tickInterval = niceStep * magnitude

    const domainMax = Math.ceil(maxValue / tickInterval) * tickInterval

    const ticks: number[] = []
    for (let i = 0; i <= domainMax; i += tickInterval) {
      ticks.push(i)
    }

    return { domain: [0, domainMax] as [number, number], ticks }
  }, [monthlyData])

  return (
    <div className="space-y-8">
      {/* Input Controls */}
      <Card>
        <CardHeader>
          <CardTitle>Forecast Parameters</CardTitle>
          <CardDescription>Adjust your assumptions to see projected portfolio growth</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-4">
            {/* Forecast Years */}
            <div className="space-y-2">
              <label className="text-sm font-medium">
                Forecast Period
              </label>
              <div className="flex items-center gap-2">
                <input
                  type="range"
                  value={forecastYears}
                  onChange={(e) => setForecastYears(Number(e.target.value))}
                  className="flex-1 h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer dark:bg-gray-700"
                  min="1"
                  max="30"
                  step="1"
                />
                <span className="text-sm font-semibold text-muted-foreground min-w-[60px] text-right">{forecastYears} {forecastYears === 1 ? 'year' : 'years'}</span>
              </div>
            </div>

            {/* Monthly Contribution */}
            <div className="space-y-2">
              <label className="text-sm font-medium">
                Monthly Contribution
              </label>
              <div className="flex items-center gap-2">
                <input
                  type="number"
                  value={monthlyContribution}
                  onChange={(e) => {
                    const val = Number(e.target.value)
                    if (!isNaN(val) && isFinite(val) && val >= 0 && val <= 1000000) {
                      setMonthlyContribution(val)
                    }
                  }}
                  className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background file:border-0 file:bg-transparent file:text-sm file:font-medium placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
                  min="0"
                  max="1000000"
                  step="100"
                />
                <span className="text-sm text-muted-foreground">{baseCurrency}</span>
              </div>
            </div>

            {/* Expected Return */}
            <div className="space-y-2">
              <label className="text-sm font-medium">
                Expected Annual Return
              </label>
              <div className="flex items-center gap-2">
                <input
                  type="number"
                  value={expectedReturn}
                  onChange={(e) => {
                    const val = Number(e.target.value)
                    if (!isNaN(val) && isFinite(val) && val >= 0 && val <= 30) {
                      setExpectedReturn(val)
                    }
                  }}
                  className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background file:border-0 file:bg-transparent file:text-sm file:font-medium placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
                  min="0"
                  max="30"
                  step="0.5"
                />
                <span className="text-sm text-muted-foreground">%</span>
              </div>
            </div>

            {/* Starting Point */}
            <div className="space-y-2">
              <label className="text-sm font-medium">
                Starting Point
              </label>
              <div className="flex items-center gap-2 h-10">
                <button
                  onClick={() => setStartFromZero(false)}
                  className={`flex-1 h-full rounded-md border text-sm font-medium transition-colors ${
                    !startFromZero
                      ? 'bg-primary text-primary-foreground'
                      : 'bg-background hover:bg-accent hover:text-accent-foreground'
                  }`}
                >
                  Current ({summaryError ? 'unavailable' : `${curSym}${formatCount(Math.round(current.startValue))}`})
                </button>
                <button
                  onClick={() => setStartFromZero(true)}
                  className={`flex-1 h-full rounded-md border text-sm font-medium transition-colors ${
                    startFromZero
                      ? 'bg-primary text-primary-foreground'
                      : 'bg-background hover:bg-accent hover:text-accent-foreground'
                  }`}
                >
                  {curSym}0
                </button>
              </div>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Projection Chart */}
      <Card>
        <CardHeader>
          <CardTitle>Projected Portfolio Growth ({forecastYears} {forecastYears === 1 ? 'Year' : 'Years'})</CardTitle>
          <CardDescription>
            Based on {expectedReturn}% annual return and {curSym}{formatCount(monthlyContribution)} monthly contribution
          </CardDescription>
        </CardHeader>
        <CardContent className="pt-6">
          {/* Height in CSS on the wrapper: 640px was 76% of an 844px phone screen. */}
          <div className="h-[280px] w-full sm:h-[440px] lg:h-[640px]">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={monthlyData} margin={{ top: 8, right: 8, left: 0, bottom: 8 }}>
              <CartesianGrid strokeDasharray="3 3" className="opacity-30" />
              <XAxis
                dataKey="year"
                label={{ value: 'Years', position: 'insideBottom', offset: -5 }}
                tick={{ fontSize: 12 }}
              />
              <YAxis
                domain={yAxisConfig.domain}
                ticks={yAxisConfig.ticks}
                tickFormatter={(value) => `${curSym}${(value / 1000).toFixed(0)}k`}
                tick={{ fontSize: 12 }}
                width={isCompact ? 48 : 80}
              />
              <Tooltip
                formatter={(value: number | undefined) => value != null ? `${curSym}${formatCount(value)}` : '—'}
                labelFormatter={(label) => `Year ${label}`}
              />
              {/* This two-series area had no legend at all, at any width — the grey band
                  and the green band were unlabelled. Unreadable is not a mobile problem,
                  but it is a problem. */}
              <Legend wrapperStyle={{ fontSize: 12 }} />
              {/* Two independent bands, deliberately NOT stacked: green is the projected
                  value and grey is the money paid in to reach it, so the gap between them
                  is the gain. Stacking would draw value + money in. The two `stackId`s
                  this used to carry were distinct, so nothing ever stacked — the rendering
                  was right and the comment calling it "stacked" was wrong.

                  The grey band is omitted, not drawn at zero, when the baseline could not
                  be loaded; the sentence under the chart says so. */}
              {moneyInToDate !== null && (
                <Area
                  type="monotone"
                  dataKey="moneyIn"
                  stroke="#94a3b8"
                  fill="#94a3b8"
                  name="Money In"
                />
              )}
              <Area
                type="monotone"
                dataKey="value"
                stroke="#22c55e"
                fill="#22c55e"
                name="Portfolio Value"
              />
            </AreaChart>
          </ResponsiveContainer>
          </div>
          {/* Prose, not a tooltip: a caveat reachable only by hovering does not exist on a
              phone. Which sentence depends on what the grey band is built from. Nothing is
              said while the baseline is still loading — the refusal is for a load that
              failed, and flashing it on every visit would teach the reader to skip it. */}
          {startFromZero ? (
            <p className="mt-3 text-xs text-muted-foreground">
              Starting from zero: <span className="font-medium text-foreground">Money In</span> is your
              monthly contributions alone, so Investment Gains is only what the projection adds.
            </p>
          ) : moneyInToDate !== null ? (
            <p className="mt-3 text-xs text-muted-foreground">
              <span className="font-medium text-foreground">Money In</span> starts at what you have paid
              in so far ({curSym}{formatCount(Math.round(moneyInToDate))}) and grows by your monthly
              contribution. The gap to <span className="font-medium text-foreground">Portfolio Value</span>{' '}
              at year 0 is the gain or loss already made.
            </p>
          ) : contributionsPending ? null : (
            <p
              role="alert"
              className="mt-3 rounded-md border border-yellow-600/40 bg-yellow-600/10 px-3 py-2 text-xs text-yellow-700 dark:text-yellow-500"
            >
              Money in to date isn't available, so Money In and Investment Gains aren't shown.
              Portfolio Value is unaffected.
            </p>
          )}
        </CardContent>
      </Card>

      {/* Projection Table */}
      <Card>
        <CardHeader>
          <CardTitle>Future Value Projections</CardTitle>
          <CardDescription>
            Portfolio growth milestones. At every horizon, Portfolio Value is Money In plus Investment Gains.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <DataTable
            rows={projections}
            columns={projectionColumns(curSym)}
            getRowKey={(proj) => proj.year}
            label="Projection by time horizon"
            density="comfortable"
          />
        </CardContent>
      </Card>

      {/* Scenario Comparison */}
      <Card>
        <CardHeader>
          <CardTitle>Scenario Comparison ({forecastYears}-Year Outlook)</CardTitle>
          <CardDescription>Different return rate scenarios</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="space-y-4">
            {scenarios.map((scenario) => (
              <div key={scenario.name} className="flex items-center justify-between p-4 rounded-lg border">
                <div>
                  <div className="font-medium">{scenario.name}</div>
                  <div className="text-sm text-muted-foreground">{scenario.rate}% annual return</div>
                </div>
                <div className="text-right">
                  <div className="text-2xl font-bold text-green-600 dark:text-green-400">
                    {curSym}{formatCount(scenario.value)}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
