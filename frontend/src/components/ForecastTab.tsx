import { CHART_TOOLTIP_STYLE, CHART_GRID_PROPS } from '@/lib/chartTooltip'
import { useState, useMemo, useEffect } from 'react'
import {
  ChartArea, Milestone, Rocket, Scale, Shield, SlidersHorizontal, Split, Sprout, Telescope, Wallet,
  type LucideIcon,
} from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { KpiCard, KpiPanel } from '@/components/ui/KpiCard'
import { SectionHeader } from '@/components/ui/SectionHeader'
import { chartColor, NEUTRAL_COLOR } from '@/lib/chartPalette'
import { XAxis, YAxis, CartesianGrid, Legend, Tooltip, ResponsiveContainer, Area, AreaChart } from 'recharts'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { summaryQuery } from '@/lib/queryCache'
import { formatAxisMoney } from '@/lib/utils'
import { niceTicks } from '@/lib/niceTicks'
import { useIsCompact } from '@/lib/useMediaQuery'
import { forecastBaseline, forecastSeries, monthlyPace, projectForecast } from '@/lib/forecast'
import { useBaseCurrency, useCurrencySymbol, useFormatMoneyWhole } from '@/lib/CurrencyContext'
import { readStored, writeStored } from '@/lib/storage'
import { DataTable, type Column } from '@/components/ui/DataTable'
import { SegmentedControl } from '@/components/ui/SegmentedControl'

const STORAGE_KEYS = {
  /** Written only when the owner types a contribution; absent means "follow my real pace". */
  chosenMonthlyContribution: 'forecast.chosenMonthlyContribution',
  expectedReturn: 'forecast.expectedReturn',
  startFromZero: 'forecast.startFromZero',
  forecastYears: 'forecast.forecastYears',
}

/**
 * The key the contribution lived under until 2026-10-14. An effect wrote it back on every
 * mount, so a stored `1000` is the old default echoed into storage, not something anybody
 * chose — and every browser that ever opened the tab has one.
 */
const LEGACY_CONTRIBUTION_KEY = 'forecast.monthlyContribution'
const LEGACY_DEFAULT_CONTRIBUTION = 1000
/** Used when the real pace is unknown: contributions failed to load, or carry no 12m window. */
const FALLBACK_CONTRIBUTION = 1000
const MAX_CONTRIBUTION = 1_000_000

function readValid(key: string, min: number, max: number): number | null {
  const saved = readStored(key)
  if (!saved) return null
  const val = Number(saved)
  if (!isFinite(val) || isNaN(val) || val < min || val > max) return null
  return val
}

function readNumber(key: string, fallback: number, min: number, max: number): number {
  return readValid(key, min, max) ?? fallback
}

/**
 * The contribution the owner deliberately chose, or `null` to follow their real pace.
 * Migrates the legacy key once: any value other than the auto-written default was typed by
 * someone and is kept as a choice; the default itself is dropped. Idempotent, so a second
 * call (StrictMode) reads the migrated key.
 */
function readChosenContribution(): number | null {
  const chosen = readValid(STORAGE_KEYS.chosenMonthlyContribution, 0, MAX_CONTRIBUTION)
  if (chosen !== null) return chosen
  const legacy = readValid(LEGACY_CONTRIBUTION_KEY, 0, MAX_CONTRIBUTION)
  if (legacy !== null && legacy !== LEGACY_DEFAULT_CONTRIBUTION) {
    writeStored(STORAGE_KEYS.chosenMonthlyContribution, String(legacy))
  }
  writeStored(LEGACY_CONTRIBUTION_KEY, null)
  return legacy === LEGACY_DEFAULT_CONTRIBUTION ? null : legacy
}

interface ProjectionRow {
  year: number
  value: number
  moneyIn: number | null
  gains: number | null
}

/** The horizons the table reports — fixed, independent of the chart's slider. */
const HORIZON_YEARS = [1, 5, 10, 15, 20]

const SCENARIOS: { name: string; rate: number; icon: LucideIcon }[] = [
  { name: 'Conservative', rate: 5, icon: Shield },
  { name: 'Moderate', rate: 8, icon: Scale },
  { name: 'Aggressive', rate: 12, icon: Rocket },
]

/** The projection's own colour, and the reference band's: the palette's first slot and grey. */
const VALUE_COLOR = chartColor(0)
const MONEY_IN_COLOR = NEUTRAL_COLOR

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
const projectionColumns = (money: (value: number) => string): Column<ProjectionRow>[] => [
  {
    key: 'horizon',
    header: 'Horizon',
    shortHeader: 'Horizon',
    mobile: 'title',
    cellClassName: 'font-medium',
    cell: (p) => `${p.year} ${p.year === 1 ? 'year' : 'years'}`,
  },
  {
    key: 'value',
    header: 'Portfolio value',
    shortHeader: 'Portfolio value',
    align: 'right',
    mobile: 'value',
    cellClassName: 'font-semibold',
    cell: (p) => money(p.value),
  },
  {
    key: 'gains',
    header: 'Gains',
    shortHeader: 'Gains',
    align: 'right',
    mobile: 'delta',
    // A book worth less than was paid in is a loss today.
    tone: (p) =>
      p.gains !== null && p.gains < 0 ? 'text-red-600 dark:text-red-400' : 'text-green-600 dark:text-green-400',
    hint: { description: 'Value minus money in' },
    cell: (p) => (p.gains === null ? '—' : money(p.gains)),
  },
  {
    key: 'moneyIn',
    header: 'Money in',
    shortHeader: 'Money in',
    align: 'right',
    hint: { description: 'Paid in plus projected contributions' },
    cell: (p) => (p.moneyIn === null ? '—' : money(p.moneyIn)),
  },
]

export function ForecastTab() {
  const curSym = useCurrencySymbol()
  const isCompact = useIsCompact()
  const money = useFormatMoneyWhole()
  const { baseCurrency } = useBaseCurrency()
  const [chosenContribution, setChosenContribution] = useState(readChosenContribution)
  const [expectedReturn, setExpectedReturn] = useState(() => readNumber(STORAGE_KEYS.expectedReturn, 8, 0, 30))
  const [startFromZero, setStartFromZero] = useState(() => {
    const saved = readStored(STORAGE_KEYS.startFromZero)
    return saved === 'true'
  })
  const [forecastYears, setForecastYears] = useState(() => readNumber(STORAGE_KEYS.forecastYears, 10, 1, 30))

  // The contribution is stored on an edit and never on mount: writing it back on mount is
  // what made the old default indistinguishable from a choice. `null` follows the real pace.
  const chooseContribution = (value: number | null) => {
    setChosenContribution(value)
    writeStored(STORAGE_KEYS.chosenMonthlyContribution, value === null ? null : String(value))
  }

  // Persist whenever values change, through the one guarded store (`lib/storage.ts`).
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
  const { data: summary, isError: summaryError } = useQuery(summaryQuery)
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

  const pace = monthlyPace(contributions)
  const monthlyContribution = chosenContribution ?? pace ?? FALLBACK_CONTRIBUTION
  const followingPace = chosenContribution === null && pace !== null

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
      SCENARIOS.map(({ name, rate, icon }) => ({
        name,
        rate,
        icon,
        value: projectForecast({ ...inputs, annualReturnPct: rate }, forecastYears * 12).value,
      })),
    [inputs, forecastYears],
  )

  // Every 6 months for a cleaner chart; the exact horizon is always the last point.
  const monthlyData = useMemo(() => forecastSeries(inputs, forecastYears * 12), [inputs, forecastYears])
  // The headline tiles read the chart's own last point — the horizon — so the figures above
  // the chart and the end of its line are one number, not two calls that could drift.
  const horizon = monthlyData[monthlyData.length - 1]
  const yearsLabel = `${forecastYears} ${forecastYears === 1 ? 'year' : 'years'}`

  // Fewer ticks on a phone: eight put nine labels about 29px apart in the 260px chart.
  const yAxisConfig = useMemo(() => {
    const maxValue = Math.max(...monthlyData.map(d => d.value), 0)
    // Nothing to scale to (no series, or a zero one): an empty 0–100k axis.
    if (!isFinite(maxValue) || maxValue <= 0) return niceTicks(0, 100000, 2, 0)
    return niceTicks(0, maxValue, isCompact ? 4 : 8, 0)
  }, [monthlyData, isCompact])

  return (
    <div className="space-y-8">
      {/* Input Controls */}
      <Card>
        <SectionHeader icon={SlidersHorizontal} title="Forecast Parameters" />
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
                  className="h-2 flex-1 cursor-pointer appearance-none rounded-full bg-muted accent-[hsl(var(--primary))]"
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
                    if (!isNaN(val) && isFinite(val) && val >= 0 && val <= MAX_CONTRIBUTION) {
                      chooseContribution(val)
                    }
                  }}
                  aria-label="Monthly Contribution"
                  className="flex h-10 w-full rounded-lg border border-input bg-card px-3 py-2 text-sm ring-offset-background file:border-0 file:bg-transparent file:text-sm file:font-medium placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
                  min="0"
                  max={MAX_CONTRIBUTION}
                  step="100"
                />
                <span className="text-sm text-muted-foreground">{baseCurrency}</span>
              </div>
              {pace !== null && (
                <button
                  type="button"
                  onClick={() => chooseContribution(null)}
                  aria-pressed={followingPace}
                  title="12M average money in per month"
                  className={`rounded-full border px-2.5 py-1 text-xs font-medium tabular-nums transition-colors ${
                    followingPace
                      ? 'bg-primary text-primary-foreground'
                      : 'bg-card hover:bg-muted'
                  }`}
                >
                  My pace · {money(pace)}
                </button>
              )}
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
                  className="flex h-10 w-full rounded-lg border border-input bg-card px-3 py-2 text-sm ring-offset-background file:border-0 file:bg-transparent file:text-sm file:font-medium placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
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
              {/* The app's one toggle group (`SegmentedControl`): the pair was two bare
                  buttons with no pressed state for assistive technology, split 50/50 so the
                  amount truncated to "Current (CHF 144…". Each option now takes its width. */}
              <SegmentedControl<'current' | 'zero'>
                label="Starting point"
                value={startFromZero ? 'zero' : 'current'}
                onChange={(v) => setStartFromZero(v === 'zero')}
                className="flex h-10 w-full"
                buttonClassName="h-9 flex-auto whitespace-nowrap"
                options={[
                  {
                    value: 'current',
                    label: `Current (${summaryError ? 'unavailable' : money(current.startValue)})`,
                  },
                  { value: 'zero', label: money(0) },
                ]}
              />
            </div>
          </div>
        </CardContent>
      </Card>

      {/* The horizon in three figures. Money in and gains are absent, not zero, when the
          baseline could not be loaded — the alert under the chart says so. */}
      {horizon && (
        <KpiPanel columns={4}>
          <KpiCard
            tile
            hero
            icon={Telescope}
            label={`Value in ${yearsLabel}`}
            value={money(horizon.value)}
            sparkline={monthlyData.map((d) => d.value)}
          />
          <KpiCard
            tile
            icon={Wallet}
            label="Money in"
            value={horizon.moneyIn === null ? null : money(horizon.moneyIn)}
          />
          <KpiCard
            tile
            icon={Sprout}
            label="Gains"
            tone={horizon.gains === null ? 'neutral' : horizon.gains < 0 ? 'negative' : 'positive'}
            value={horizon.gains === null ? null : money(horizon.gains)}
          />
        </KpiPanel>
      )}

      {/* Projection Chart */}
      <Card>
        <SectionHeader
          icon={ChartArea}
          title="Projected growth"
          description={
            <>{expectedReturn}% a year · {money(monthlyContribution)} a month · {yearsLabel}</>
          }
        />
        <CardContent className="pt-2">
          {/* Height in CSS on the wrapper: 640px was 76% of an 844px phone screen. */}
          <div className="h-[260px] w-full sm:h-[360px] lg:h-[420px]">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={monthlyData} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
              <defs>
                <linearGradient id="forecast-value-fill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" style={{ stopColor: VALUE_COLOR, stopOpacity: 0.25 }} />
                  <stop offset="100%" style={{ stopColor: VALUE_COLOR, stopOpacity: 0.02 }} />
                </linearGradient>
              </defs>
              <CartesianGrid {...CHART_GRID_PROPS} />
              <XAxis
                dataKey="year"
                axisLine={false}
                tickLine={false}
                tickMargin={8}
                tick={{ fill: 'hsl(var(--muted-foreground))' }}
                // Whole years only; the series is monthly.
                ticks={monthlyData.filter((d) => d.year.endsWith('.0')).map((d) => d.year)}
                tickFormatter={(year: string) => `${Number(year)}y`}
              />
              <YAxis
                domain={yAxisConfig.domain}
                ticks={yAxisConfig.ticks}
                tickFormatter={(value: number) => formatAxisMoney(value, curSym, yAxisConfig.step)}
                axisLine={false}
                tickLine={false}
                tick={{ fill: 'hsl(var(--muted-foreground))' }}
                width="auto"
              />
              <Tooltip
                contentStyle={CHART_TOOLTIP_STYLE}
                formatter={(value: number | undefined) => value != null ? money(value) : '—'}
                labelFormatter={(label) => `In ${Number(label)} ${Number(label) === 1 ? 'year' : 'years'}`}
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
                  stroke={MONEY_IN_COLOR}
                  strokeDasharray="4 4"
                  fill={MONEY_IN_COLOR}
                  fillOpacity={0.12}
                  name="Money In"
                />
              )}
              <Area
                type="monotone"
                dataKey="value"
                stroke={VALUE_COLOR}
                strokeWidth={2.25}
                fill="url(#forecast-value-fill)"
                name="Portfolio Value"
              />
            </AreaChart>
          </ResponsiveContainer>
          </div>
          {/* Said only when the load failed: flashing it while the baseline is still
              loading would teach the reader to skip it. */}
          {startFromZero || moneyInToDate !== null || contributionsPending ? null : (
            <p
              role="alert"
              className="mt-3 rounded-lg border border-warn/40 bg-warn-soft px-3 py-2 text-xs text-warn"
            >
              Money in to date isn't available, so Money In and Investment Gains aren't shown.
              Portfolio Value is unaffected.
            </p>
          )}
        </CardContent>
      </Card>

      {/* Projection Table */}
      <Card>
        <SectionHeader icon={Milestone} title="Projections" />
        <CardContent>
          <DataTable
            rows={projections}
            columns={projectionColumns(money)}
            getRowKey={(proj) => proj.year}
            label="Projection by time horizon"
            density="comfortable"
          />
        </CardContent>
      </Card>

      {/* Scenario Comparison */}
      <Card>
        <SectionHeader icon={Split} title={`Scenarios in ${yearsLabel}`} />
        <CardContent>
          <KpiPanel columns={3} fillLastRow className="shadow-none">
            {scenarios.map((scenario) => (
              <KpiCard
                key={scenario.name}
                tile
                icon={scenario.icon}
                label={scenario.name}
                value={money(scenario.value)}
                sub={`${scenario.rate}% a year`}
              />
            ))}
          </KpiPanel>
        </CardContent>
      </Card>
    </div>
  )
}
