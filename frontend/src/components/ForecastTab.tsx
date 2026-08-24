import { useState, useMemo, useEffect } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { XAxis, YAxis, CartesianGrid, Legend, Tooltip, ResponsiveContainer, Area, AreaChart } from 'recharts'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { formatCount } from '@/lib/utils'
import { useBaseCurrency, useCurrencySymbol } from '@/lib/CurrencyContext'
import { useIsCompact } from '@/lib/useMediaQuery'
import { DataTable, type Column } from '@/components/ui/DataTable'

const STORAGE_KEYS = {
  monthlyContribution: 'forecast.monthlyContribution',
  expectedReturn: 'forecast.expectedReturn',
  startFromZero: 'forecast.startFromZero',
  forecastYears: 'forecast.forecastYears',
}

function readNumber(key: string, fallback: number, min: number, max: number): number {
  const saved = localStorage.getItem(key)
  if (!saved) return fallback
  const val = Number(saved)
  if (!isFinite(val) || isNaN(val) || val < min || val > max) return fallback
  return val
}

/** The projection table's four columns, described once for both renderings. */
const projectionColumns = (
  curSym: string
): Column<{ year: number; futureValue: number; totalContributions: number; investmentGains: number }>[] => [
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
    cell: (p) => `${curSym}${formatCount(p.futureValue)}`,
  },
  {
    key: 'gains',
    header: 'Investment Gains',
    shortHeader: 'Investment gains',
    align: 'right',
    mobile: 'delta',
    tone: () => 'text-blue-600 dark:text-blue-400',
    cell: (p) => `${curSym}${formatCount(p.investmentGains)}`,
  },
  {
    key: 'contributions',
    header: 'Total Contributions',
    shortHeader: 'Total contributions',
    align: 'right',
    cell: (p) => `${curSym}${formatCount(p.totalContributions)}`,
  },
]

export function ForecastTab() {
  const curSym = useCurrencySymbol()
  const isCompact = useIsCompact()
  const { baseCurrency } = useBaseCurrency()
  const [monthlyContribution, setMonthlyContribution] = useState(() => readNumber(STORAGE_KEYS.monthlyContribution, 1000, 0, 1000000))
  const [expectedReturn, setExpectedReturn] = useState(() => readNumber(STORAGE_KEYS.expectedReturn, 8, 0, 30))
  const [startFromZero, setStartFromZero] = useState(() => {
    const saved = localStorage.getItem(STORAGE_KEYS.startFromZero)
    return saved === 'true'
  })
  const [forecastYears, setForecastYears] = useState(() => readNumber(STORAGE_KEYS.forecastYears, 10, 1, 30))

  // Save to localStorage whenever values change
  useEffect(() => {
    localStorage.setItem(STORAGE_KEYS.monthlyContribution, monthlyContribution.toString())
  }, [monthlyContribution])

  useEffect(() => {
    localStorage.setItem(STORAGE_KEYS.expectedReturn, expectedReturn.toString())
  }, [expectedReturn])

  useEffect(() => {
    localStorage.setItem(STORAGE_KEYS.startFromZero, startFromZero.toString())
  }, [startFromZero])

  useEffect(() => {
    localStorage.setItem(STORAGE_KEYS.forecastYears, forecastYears.toString())
  }, [forecastYears])

  // Fetch current portfolio value
  const { data: summary, isError: summaryError } = useQuery({
    queryKey: ['portfolio', 'summary'],
    queryFn: () => api.getPortfolioSummary(),
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
  const currentValue = startFromZero ? 0 : (summary?.total_market_value_eur ?? 0)

  // Calculate projections
  const projections = useMemo(() => {
    const years = [1, 5, 10, 15, 20]
    const monthlyRate = expectedReturn / 100 / 12

    return years.map(year => {
      const months = year * 12

      // Future Value = PV(1+r)^t + PMT × [(1+r)^t - 1] / r
      const portfolioGrowth = currentValue * Math.pow(1 + monthlyRate, months)

      // Handle division by zero when monthlyRate is 0
      let contributionsGrowth = 0
      if (monthlyRate === 0) {
        contributionsGrowth = monthlyContribution * months
      } else {
        contributionsGrowth = monthlyContribution *
          ((Math.pow(1 + monthlyRate, months) - 1) / monthlyRate)
      }

      const futureValue = portfolioGrowth + contributionsGrowth
      const totalContributions = monthlyContribution * months
      const investmentGains = futureValue - currentValue - totalContributions

      // Guard against invalid numbers
      const safeValue = (val: number) => (!isFinite(val) || isNaN(val)) ? 0 : val

      return {
        year,
        futureValue: Math.round(safeValue(futureValue)),
        totalContributions: Math.round(safeValue(totalContributions)),
        investmentGains: Math.round(safeValue(investmentGains)),
        portfolioGrowth: Math.round(safeValue(portfolioGrowth)),
      }
    })
  }, [currentValue, monthlyContribution, expectedReturn])

  // Scenario comparison
  const scenarios = useMemo(() => {
    const months = forecastYears * 12
    const rates = [
      { name: 'Conservative', rate: 5 },
      { name: 'Moderate', rate: 8 },
      { name: 'Aggressive', rate: 12 },
    ]

    return rates.map(({ name, rate }) => {
      const monthlyRate = rate / 100 / 12
      const portfolioGrowth = currentValue * Math.pow(1 + monthlyRate, months)

      // Handle division by zero when monthlyRate is 0
      let contributionsGrowth = 0
      if (monthlyRate === 0) {
        contributionsGrowth = monthlyContribution * months
      } else {
        contributionsGrowth = monthlyContribution *
          ((Math.pow(1 + monthlyRate, months) - 1) / monthlyRate)
      }

      const futureValue = portfolioGrowth + contributionsGrowth
      const safeValue = !isFinite(futureValue) || isNaN(futureValue) ? 0 : futureValue

      return {
        name,
        rate,
        value: Math.round(safeValue),
      }
    })
  }, [currentValue, monthlyContribution, forecastYears])

  // Monthly progression data for chart
  const monthlyData = useMemo(() => {
    const months = forecastYears * 12
    const monthlyRate = expectedReturn / 100 / 12
    const data = []

    for (let month = 0; month <= months; month += 6) { // Every 6 months for cleaner chart
      const portfolioGrowth = currentValue * Math.pow(1 + monthlyRate, month)

      // Handle division by zero when monthlyRate is 0
      let contributionsGrowth = 0
      if (month > 0) {
        if (monthlyRate === 0) {
          contributionsGrowth = monthlyContribution * month
        } else {
          contributionsGrowth = monthlyContribution * ((Math.pow(1 + monthlyRate, month) - 1) / monthlyRate)
        }
      }

      const futureValue = portfolioGrowth + contributionsGrowth
      const totalContributions = currentValue + (monthlyContribution * month)

      // Guard against invalid numbers
      const safeValue = (val: number) => (!isFinite(val) || isNaN(val)) ? 0 : val

      data.push({
        month,
        year: (month / 12).toFixed(1),
        value: Math.round(safeValue(futureValue)),
        contributions: Math.round(safeValue(totalContributions)),
      })
    }

    // Ensure the final data point reaches the exact endpoint
    if (data.length > 0 && data[data.length - 1].month !== months) {
      const portfolioGrowth = currentValue * Math.pow(1 + monthlyRate, months)
      let contributionsGrowth = 0
      if (monthlyRate === 0) {
        contributionsGrowth = monthlyContribution * months
      } else {
        contributionsGrowth = monthlyContribution * ((Math.pow(1 + monthlyRate, months) - 1) / monthlyRate)
      }
      const futureValue = portfolioGrowth + contributionsGrowth
      const totalContributions = currentValue + (monthlyContribution * months)
      const safeValue = (val: number) => (!isFinite(val) || isNaN(val)) ? 0 : val
      data.push({
        month: months,
        year: (months / 12).toFixed(1),
        value: Math.round(safeValue(futureValue)),
        contributions: Math.round(safeValue(totalContributions)),
      })
    }

    return data
  }, [currentValue, monthlyContribution, expectedReturn, forecastYears])

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
                  Current ({summaryError ? 'unavailable' : `${curSym}${formatCount(Math.round(currentValue))}`})
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
              {/* This two-series stacked area had no legend at all, at any width — the
                  grey band and the green band were unlabelled. Unreadable is not a
                  mobile problem, but it is a problem. */}
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <Area
                type="monotone"
                dataKey="contributions"
                stackId="1"
                stroke="#94a3b8"
                fill="#94a3b8"
                name="Total Contributions"
              />
              <Area
                type="monotone"
                dataKey="value"
                stackId="2"
                stroke="#22c55e"
                fill="#22c55e"
                name="Portfolio Value"
              />
            </AreaChart>
          </ResponsiveContainer>
          </div>
        </CardContent>
      </Card>

      {/* Projection Table */}
      <Card>
        <CardHeader>
          <CardTitle>Future Value Projections</CardTitle>
          <CardDescription>Portfolio growth milestones</CardDescription>
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
