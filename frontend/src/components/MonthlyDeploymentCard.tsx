import { useState } from 'react'
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ReferenceLine,
  ResponsiveContainer,
} from 'recharts'
import { Card, CardContent } from '@/components/ui/card'
import { CollapsibleCardHeader } from '@/components/ui/CollapsibleCardHeader'
import { useCurrencySymbol } from '@/lib/CurrencyContext'
import type { ContributionsResponse, ContributionMonthlyItem } from '@/lib/api'
import { useIsCompact } from '@/lib/useMediaQuery'

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

// Money in and deployed are the same measure family, so two steps of one hue rather
// than two hues. This exact pair passes the palette validator's CVD-separation, chroma
// and lightness checks on both the light and dark surfaces. The darker step carries
// money in, which is now the primary series.
const MONEY_IN_COLOR = '#1d4fd8'
const DEPLOYED_COLOR = '#4a90f7'

type ChartRow = ContributionMonthlyItem & { label: string }

/** Whole units — cents on a monthly figure are noise, and the tooltip keeps them. */
function whole(value: number): string {
  return value.toLocaleString('en-US', { maximumFractionDigits: 0 })
}

/**
 * Money in per month, with capital deployed beside it.
 *
 * **Money in leads and deployed is the context** — it did not until 2026-09-06, and a
 * rotation is what made that matter. Selling the Ireland-domiciled sleeve to buy US
 * ETFs deployed ~31k CHF in one month against a few hundred francs of new money, and
 * the only series on the chart was the gross one: a bar four times the height of any
 * contribution this account has ever made, in a month nothing was paid in. Deployed was
 * not wrong — it is defined to count a rotation twice, because the gap between the two
 * IS the churn measurement — it was simply alone.
 *
 * So `money_in_eur` is the primary bar, on the same era splice the strip's headline and
 * the value chart's Money In line use (`_contribution_inputs`: one event list, three
 * readers, pinned equal in `test_cash_balance.py`). Deployed stays as a second bar so
 * the gap is readable without hovering, and `net_eur` moved to the tooltip, which is
 * where its own server-side docstring already said it belonged.
 */
export function MonthlyDeploymentCard({ data, isLoading, isError }: MonthlyDeploymentCardProps) {
  const curSym = useCurrencySymbol()
  const isCompact = useIsCompact()
  const [open, setOpen] = useState(false)

  const monthly = data?.monthly ?? []
  const chartData: ChartRow[] = monthly.map(m => ({ ...m, label: monthLabel(m.month) }))

  // Absent means "this backend does not publish per-month money in", never "nothing was
  // paid in" — the same backward-compatible reading `unpriced_holdings` and
  // `externalFlow` make about their own optional fields. Falling back to the
  // deployment-only chart is honest; drawing a row of zeros would not be.
  const hasMoneyIn = monthly.length > 0 && monthly.every(m => typeof m.money_in_eur === 'number')

  // Under the 'deployed' method there is no deposit ledger, so money in IS deployment
  // and a second identical bar would be pure noise. Same condition, for the same reason,
  // that suppresses the strip's `/deployed` suffix.
  const hasLedger = data?.windows.some(w => w.money_in_method !== 'deployed') ?? false
  const showDeployed = !hasMoneyIn || hasLedger

  const legendItems = [
    ...(hasMoneyIn ? [{ label: 'Money in', color: MONEY_IN_COLOR }] : []),
    ...(showDeployed ? [{ label: 'Deployed', color: DEPLOYED_COLOR }] : []),
  ]

  // The largest month where deployment exceeded money in, named below the chart. The
  // largest rather than the latest, so the note cannot vanish while the spike it
  // explains is still on screen, and so it points at the bar being looked at.
  let rotation: { row: ChartRow; gap: number } | null = null
  if (hasMoneyIn && hasLedger) {
    for (const row of chartData) {
      const gap = row.deployed_eur - (row.money_in_eur ?? 0)
      if (gap > 0.005 && (rotation === null || gap > rotation.gap)) rotation = { row, gap }
    }
  }

  // Collapsed summary: last month + the 12M average.
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
  let summaryText: React.ReactNode = hasMoneyIn ? 'Money in per month' : 'Capital deployed per month'
  if (isError) {
    summaryText = 'Could not load contributions'
  } else if (monthly.length > 0) {
    const last = monthly[monthly.length - 1]
    summaryText = (
      <>
        {monthLabel(last.month)}: {curSym}
        {hasMoneyIn
          ? `${whole(last.money_in_eur ?? 0)} in`
          : `${whole(last.deployed_eur)} deployed`}
        {twelveMonth && (
          <>
            {' · '}
            {/* A four-month-old portfolio has no twelve-month average, and the server
                already refuses to pretend otherwise by clamping the divisor. Naming the
                window it actually measured is the matching honesty on this side. */}
            {twelveMonth.partial ? `${twelveMonth.months.toFixed(0)}M avg` : '12M avg'}: {curSym}
            {whole(hasMoneyIn
              ? twelveMonth.avg_money_in_per_month_eur
              : twelveMonth.avg_deployed_per_month_eur)}/mo
          </>
        )}
      </>
    )
  }

  return (
    <Card>
      <CollapsibleCardHeader
        open={open}
        onToggle={() => setOpen(o => !o)}
        title="Money In per Month"
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
              Couldn't load contributions — the backend didn't respond. It retries automatically.
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
                    <CartesianGrid strokeDasharray="3 3" vertical={false} />
                    <XAxis
                      dataKey="label"
                      fontSize={12}
                      tick={{ fill: 'currentColor' }}
                      interval="preserveStartEnd"
                      minTickGap={isCompact ? 28 : 8}
                    />
                    <YAxis
                      fontSize={12}
                      tickFormatter={(value: number) => {
                        if (Math.abs(value) >= 1000) {
                          return `${(value / 1000).toFixed(1)}k`
                        }
                        return value.toFixed(0)
                      }}
                    />
                    <Tooltip content={<DeploymentTooltip hasMoneyIn={hasMoneyIn} />} />
                    {/* Rendered rather than left to Recharts, which derives its own order
                        from the children in a way that does not match the order the bars
                        are DRAWN in: declared money-in-first, the legend still came out
                        "Deployed, Money in" while the leftmost bar of each pair was money
                        in. Two orders for one pair of series, and the wrong one puts the
                        secondary first. (`payload` is not in this version's Legend props,
                        so `content` is the typed way to say it.) */}
                    <Legend content={<SeriesLegend items={legendItems} />} />
                    {/* Money in goes negative in a withdrawal month, so the zero line stays. */}
                    <ReferenceLine y={0} stroke="hsl(var(--muted-foreground))" strokeWidth={1} />
                    {hasMoneyIn && (
                      <Bar dataKey="money_in_eur" name="Money in" fill={MONEY_IN_COLOR} radius={[4, 4, 0, 0]} />
                    )}
                    {showDeployed && (
                      <Bar dataKey="deployed_eur" name="Deployed" fill={DEPLOYED_COLOR} radius={[4, 4, 0, 0]} />
                    )}
                  </BarChart>
                </ResponsiveContainer>
              </div>
              {/* Inside the collapsible body deliberately. The MonthlyReturnsHeatmap rule —
                  a caveat only reachable by expanding is as good as absent — bites when the
                  COLLAPSED SUMMARY publishes the qualified figure. This summary publishes
                  money in, which needs no qualification; the note explains the second bar,
                  so it belongs beside it. */}
              {rotation && (
                <p className="mt-2 text-xs text-muted-foreground">
                  Deployed exceeds money in wherever capital was rotated between holdings —
                  selling one holding to buy another puts the same money to work twice.{' '}
                  {rotation.row.label} is the largest: {curSym}{whole(rotation.gap)} of the{' '}
                  {curSym}{whole(rotation.row.deployed_eur)} deployed was capital already
                  invested, not new money.
                </p>
              )}
            </>
          )}
        </CardContent>
      )}
    </Card>
  )
}

/** Legend in declaration order, so it reads left-to-right like the bars do. */
function SeriesLegend({ items }: { items: Array<{ label: string; color: string }> }) {
  return (
    <ul className="flex flex-wrap justify-center gap-x-4 gap-y-1 text-xs">
      {items.map(i => (
        <li key={i.label} className="flex items-center gap-1.5">
          <span
            className="inline-block h-2.5 w-2.5 shrink-0 rounded-[2px]"
            style={{ backgroundColor: i.color }}
            aria-hidden="true"
          />
          <span>{i.label}</span>
        </li>
      ))}
    </ul>
  )
}

function DeploymentTooltip(
  { active, payload, hasMoneyIn }:
  { active?: boolean; payload?: Array<{ payload: ChartRow }>; hasMoneyIn?: boolean }
) {
  const curSym = useCurrencySymbol()
  if (!active || !payload || payload.length === 0) return null

  const d = payload[0].payload
  const released = d.deployed_eur - d.net_eur
  const fmt = (v: number) =>
    `${curSym}${v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

  return (
    <div className="bg-popover border rounded-lg shadow-md p-3 text-sm space-y-1">
      <p className="font-semibold">{d.label}</p>
      <div className="border-t pt-1 mt-1 space-y-0.5">
        {hasMoneyIn && (
          <p>
            <span className="text-muted-foreground">Money in: </span>
            <span className="font-medium">{fmt(d.money_in_eur ?? 0)}</span>
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
