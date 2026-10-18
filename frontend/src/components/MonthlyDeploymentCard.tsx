import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ReferenceLine,
  ResponsiveContainer,
} from 'recharts'
import { BanknoteArrowUp } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { chartColor } from '@/lib/chartPalette'
import { CollapsibleCardHeader } from '@/components/ui/CollapsibleCardHeader'
import { useFormatCurrency, useFormatMoneyWhole } from '@/lib/CurrencyContext'
import type { ContributionsResponse, ContributionMonthlyItem } from '@/lib/api'
import { useIsCompact } from '@/lib/useMediaQuery'
import { ABSENT, formatAxisMoney } from '@/lib/utils'
import { zeroBasedTicks } from '@/lib/niceTicks'
import { useRememberedOpen } from '@/lib/storage'
import { CHART_GRID_PROPS } from '@/lib/chartTooltip'

interface MonthlyDeploymentCardProps {
  data: ContributionsResponse | undefined
  isLoading: boolean
  isError?: boolean
}

const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

// "2026-07" -> "Jul 26"
function monthLabel(month: string): string {
  const [y, m] = month.split('-')
  return `${MONTH_NAMES[parseInt(m, 10) - 1] ?? m} ${y.slice(2)}`
}

// Only one series is ever drawn (deployed is the fallback when money in is absent), so
// both take the palette's first slot rather than hexes of their own.
const BAR_COLOR = chartColor(0)

type ChartRow = ContributionMonthlyItem & { label: string }

/** "YYYY-MM" of the reader's calendar month — the key `monthly[]` rows carry. */
function monthKey(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`
}

/**
 * Money in per month — one series, answering one question: how much new money went in.
 *
 * **The chart drew gross deployment alone until 2026-09-06**, and a rotation is what
 * made that matter. Selling the Ireland-domiciled sleeve to buy US ETFs deployed ~31k
 * CHF in one month against a few hundred francs of new money, so the chart carried a bar
 * four times the height of any contribution this account has ever made, in a month
 * nothing was paid in. Deployed was not wrong — it is defined to count a rotation twice,
 * because the gap between the two IS the churn measurement — it was simply alone.
 *
 * So the bar is `money_in_eur`, on the same era splice the strip's headline and the value
 * chart's Money In line use (`_contribution_inputs`: one event list, three readers, pinned
 * equal in `test_cash_balance.py`).
 *
 * **Deployed was a second bar beside it for one day**, and came out on 2026-09-07 at the
 * account owner's request: they read this card for their contribution rate and the second
 * series was answering a question they were not asking. It was also mostly redundant —
 * the two are the *identical* number in every month before `coverage_from`, where money in
 * IS lot cost basis, which is 20 of this account's 29 months, plus five more where they
 * differ by a few hundred francs of dividend reinvestment. It earned its ink in two months
 * out of 29, and those two are exactly what the prose note under the chart now names.
 *
 * `deployed_eur`, `net_eur` and the released figure all survive in the **tooltip**, and
 * that is not sentimentality: money in is derived from the deposit ledger and deployed
 * from tax lots, so deployed is the only independent check on the highest-risk failure
 * in this feature — a broker transfer booked as an ordinary deposit, which inflates money
 * in with nothing else on the screen able to disagree.
 */
export function MonthlyDeploymentCard({ data, isLoading, isError }: MonthlyDeploymentCardProps) {
  const formatWhole = useFormatMoneyWhole()
  const isCompact = useIsCompact()
  const [open, setOpen] = useRememberedOpen('money-in-per-month')

  const monthly = data?.monthly ?? []
  const chartData: ChartRow[] = monthly.map(m => ({ ...m, label: monthLabel(m.month) }))

  // Absent means "this backend does not publish per-month money in", never "nothing was
  // paid in" — the same backward-compatible reading `unpriced_holdings` and
  // `externalFlow` make about their own optional fields. Falling back to the
  // deployment-only chart is honest; drawing a row of zeros would not be.
  const hasMoneyIn = monthly.length > 0 && monthly.every(m => typeof m.money_in_eur === 'number')

  // Deployed is drawn ONLY as the legacy fallback, when the backend publishes no money
  // in. It was a second bar beside money in until 2026-09-07, and the account owner
  // asked for it out: the two series are the *identical* number in 20 of this account's
  // 29 months (every month before `coverage_from`, where money in IS lot cost basis) and
  // differ trivially in five more, so it was redundant ink in 25 of 29 — earning its
  // place only in the two rotation months.
  // It survives in the tooltip, which is the point: money in comes from the deposit
  // ledger and deployed from tax lots, so deployed is the one independent check on the
  // failure this feature fears most — a transfer booked as a deposit, which inflates
  // money in with nothing beside it to disagree.
  const showDeployed = !hasMoneyIn
  const yAxis = zeroBasedTicks(monthly.map(m => (hasMoneyIn ? m.money_in_eur : m.deployed_eur) ?? NaN), 4)

  // Collapsed summary: this month so far, the 12M average pace, and year-to-date money in
  // (the legacy deployment fallback keeps last month + the average).
  //
  // `monthly[]` has a row for every month with a deposit or a purchase, clamped at the
  // server's today, so a month without a row had nothing recorded: this month so far and
  // YTD are then a real 0, not an unknown. A row whose money in is missing reads "—".
  //
  // The average is READ from the server's 12m window, not recomputed here. It was
  // recomputed until 2026-08-05 as `monthly.slice(-12)` summed and divided by its own
  // length, which is the same quantity the strip directly above this card already
  // renders — two numbers under one name on one screen, 16 apart on live data.
  //
  // Both halves of the recomputation were wrong in the same direction. `monthly` omits a
  // month with no activity, so `slice(-12)` takes the last twelve *rows*, which can span
  // more than twelve months, and dividing by `window.length` then uses a divisor smaller
  // than the months covered. The server divides by the window's elapsed months, clamped
  // to available history, and says so via `partial`.
  //
  // It reads `avg_money_in_per_month_eur` now rather than `avg_deployed_per_month_eur`,
  // so this figure and the strip's are the same quantity as well as the same number.
  const twelveMonth = data?.windows.find(w => w.label === '12m')
  // Whole units — cents on a monthly figure are noise, and the tooltip keeps them.
  const money = (v: number | null | undefined) => (v == null ? ABSENT : formatWhole(v))
  let summaryText: React.ReactNode = hasMoneyIn ? 'Money in per month' : 'Capital deployed per month'
  if (isError) {
    summaryText = 'Could not load contributions'
  } else if (monthly.length > 0) {
    const today = new Date()
    const thisMonth = monthKey(today)
    const thisYear = `${today.getFullYear()}-`
    // `hasMoneyIn` means every row carries a number; a month with no row is a real 0.
    const thisMonthRow = monthly.find(m => m.month === thisMonth)
    const monthSoFar = thisMonthRow ? thisMonthRow.money_in_eur : 0
    const yearToDate = monthly
      .filter(m => m.month.startsWith(thisYear))
      .reduce<number | undefined>(
        (sum, m) => (sum === undefined || m.money_in_eur === undefined ? undefined : sum + m.money_in_eur),
        0,
      )
    const last = monthly[monthly.length - 1]
    summaryText = (
      <span className="tabular-nums">
        {hasMoneyIn
          ? `${MONTH_NAMES[today.getMonth()]} so far: ${money(monthSoFar)}`
          : `${monthLabel(last.month)}: ${money(last.deployed_eur)} deployed`}
        {twelveMonth && (
          <>
            {' · '}
            {/* A four-month-old portfolio has no twelve-month average, and the server
                already refuses to pretend otherwise by clamping the divisor. Naming the
                window it actually measured is the matching honesty on this side. */}
            {twelveMonth.partial ? `${twelveMonth.months.toFixed(0)}M avg` : '12M avg'}: {money(hasMoneyIn
              ? twelveMonth.avg_money_in_per_month_eur
              : twelveMonth.avg_deployed_per_month_eur)}/mo
          </>
        )}
        {hasMoneyIn && ` · YTD: ${money(yearToDate)}`}
      </span>
    )
  }

  return (
    <Card>
      <CollapsibleCardHeader
        open={open}
        onToggle={() => setOpen(o => !o)}
        title="Money In per Month"
        icon={BanknoteArrowUp}
        description={summaryText}
        contentId="monthly-deployment-content"
      />
      {open && (
        <CardContent id="monthly-deployment-content">
          {isLoading ? (
            <div className="h-[240px] w-full animate-pulse rounded-md bg-muted sm:h-[300px]" />
          ) : isError ? (
            // A failed fetch must not read as "you have deployed no capital" — same
            // rule as PortfolioValueChart and PerformanceAttribution.
            <p className="text-muted-foreground text-center py-8">
              Couldn't load contributions — the server didn't respond. It retries automatically.
            </p>
          ) : chartData.length === 0 ? (
            <p className="text-muted-foreground text-center py-8">
              No contribution history yet.
            </p>
          ) : (
            <>
              <div className="h-[240px] w-full sm:h-[300px]">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={chartData} margin={{ top: 5, right: 8, left: 0, bottom: 5 }}>
                    <CartesianGrid {...CHART_GRID_PROPS} />
                    <XAxis
                      dataKey="label"
                      fontSize={12}
                      tick={{ fill: 'currentColor' }}
                      interval="preserveStartEnd"
                      minTickGap={isCompact ? 28 : 8}
                    />
                    <YAxis
                      fontSize={12}
                      width="auto"
                      domain={yAxis.domain}
                      ticks={yAxis.ticks}
                      tickFormatter={(value: number) => formatAxisMoney(value, '', yAxis.step)}
                    />
                    <Tooltip content={<DeploymentTooltip hasMoneyIn={hasMoneyIn} />} />
                    {/* No legend: exactly one series is ever drawn, and a one-item legend
                        is a label for a chart that already has a title. (It carried two
                        items until 2026-09-07 — and note it had to be hand-rendered even
                        then, because Recharts derived its own order from the children and
                        came out "Deployed, Money in" while the leftmost bar of each pair
                        was money in.) */}
                    {/* Money in goes negative in a withdrawal month, so the zero line stays. */}
                    <ReferenceLine y={0} stroke="hsl(var(--muted-foreground))" strokeWidth={1} />
                    {hasMoneyIn && (
                      <Bar dataKey="money_in_eur" name="Money in" fill={BAR_COLOR} radius={[4, 4, 0, 0]} />
                    )}
                    {showDeployed && (
                      <Bar dataKey="deployed_eur" name="Deployed" fill={BAR_COLOR} radius={[4, 4, 0, 0]} />
                    )}
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </>
          )}
        </CardContent>
      )}
    </Card>
  )
}

function DeploymentTooltip(
  { active, payload, hasMoneyIn }:
  { active?: boolean; payload?: Array<{ payload: ChartRow }>; hasMoneyIn?: boolean }
) {
  const fmt = useFormatCurrency()
  if (!active || !payload || payload.length === 0) return null

  const d = payload[0].payload
  const released = d.deployed_eur - d.net_eur

  return (
    <div className="bg-popover border rounded-lg shadow-md p-3 text-sm space-y-1">
      <p className="font-semibold">{d.label}</p>
      <div className="border-t pt-1 mt-1 space-y-0.5">
        {hasMoneyIn && (
          <p>
            <span className="text-muted-foreground">Money in: </span>
            <span className="font-medium">{d.money_in_eur == null ? ABSENT : fmt(d.money_in_eur)}</span>
          </p>
        )}
        <p>
          <span className="text-muted-foreground">Deployed: </span>
          <span className="font-medium">{fmt(d.deployed_eur)}</span>
        </p>
        {released > 0.005 && (
          <p>
            <span className="text-muted-foreground">Released by sales: </span>
            <span>{fmt(released)}</span>
          </p>
        )}
        <p>
          <span className="text-muted-foreground">Net: </span>
          <span>{fmt(d.net_eur)}</span>
        </p>
      </div>
    </div>
  )
}
