import type { ReactNode } from 'react'
import { Card, CardContent } from '@/components/ui/card'
import { cashIsTracked } from '@/lib/portfolioCash'
import type { PortfolioSummary } from '@/lib/api'
import type { PeriodChange } from '@/lib/portfolioKpis'
import type { TimeRange } from '@/lib/dateRanges'
import { useBaseCurrency, useFormatCurrency } from '@/lib/CurrencyContext'
import { cn } from '@/lib/utils'

/** What the selected range is called beside the period's gain. */
const RANGE_LABEL: Record<TimeRange, string> = {
  '1W': 'past week',
  MTD: 'this month',
  '1M': 'past month',
  '3M': 'past 3 months',
  '6M': 'past 6 months',
  YTD: 'this year',
  '1Y': 'past year',
  '2Y': 'past 2 years',
  ALL: 'all time',
}

const GAIN = 'text-green-600 dark:text-green-400'
const LOSS = 'text-red-600 dark:text-red-400'
/** The period-gain pill: the gain's own colour on a wash of it. */
const GAIN_PILL = 'bg-green-500/10 text-green-600 dark:bg-green-400/10 dark:text-green-400'
const LOSS_PILL = 'bg-red-500/10 text-red-600 dark:bg-red-400/10 dark:text-red-400'

/**
 * The headline amount with its currency and cents a step quieter than the whole units,
 * the way banking apps set a balance: the eye reads "144,022" first. A screen reader gets
 * the amount as one string from the `sr-only` copy, not three fragments of it.
 */
function HeroAmount({ value, currency }: { value: number; currency: string }) {
  const parts = new Intl.NumberFormat('en-US', {
    style: 'currency', currency, minimumFractionDigits: 2, maximumFractionDigits: 2,
  }).formatToParts(value)
  const whole = parts.findIndex((p) => p.type === 'integer')
  const cents = parts.findIndex((p) => p.type === 'decimal')
  const text = (from: number, to?: number) => parts.slice(from, to).map((p) => p.value).join('')
  return (
    <span className="inline-flex items-baseline">
      <span className="sr-only">{text(0)}</span>
      <span aria-hidden="true" className="mr-1 text-[0.55em] font-medium text-muted-foreground">
        {text(0, whole).trim()}
      </span>
      <span aria-hidden="true">{text(whole, cents === -1 ? undefined : cents)}</span>
      {cents !== -1 && (
        <span aria-hidden="true" className="text-[0.55em] font-medium text-muted-foreground">
          {text(cents)}
        </span>
      )}
    </span>
  )
}

interface PortfolioHeroProps {
  summary: PortfolioSummary | undefined
  isLoading?: boolean
  isError?: boolean
  /** The range's gain — value change less net money in — or `null` while unknown. */
  period: PeriodChange | null
  range: TimeRange
  /** The range picker and benchmark control, top right. */
  controls: ReactNode
  /** The value chart. */
  children: ReactNode
}

/**
 * The top of the Performance tab: what the account is worth, what it made over the
 * selected range, the chart, and the four figures the headline is made of.
 *
 * Replaced five separate summary cards plus a chart card with its own header (2026-10-16):
 * the headline and the chart answer one question, so they share one surface. The gain is
 * the period's *profit* — the value change with deposits netted out — because a value
 * change that counts deposits reads as growth that was never earned.
 */
export function PortfolioHero({
  summary, isLoading, isError, period, range, controls, children,
}: PortfolioHeroProps) {
  const formatCurrency = useFormatCurrency()
  const { baseCurrency } = useBaseCurrency()
  const signed = (n: number) => `${n >= 0 ? '+' : ''}${formatCurrency(n)}`
  const signedPct = (n: number) => `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`

  if (isError) {
    return (
      <Card>
        <CardContent className="py-10 text-center text-muted-foreground">
          Couldn't load the portfolio. It retries automatically.
        </CardContent>
      </Card>
    )
  }

  if (!isLoading && !summary) {
    return (
      <Card>
        <CardContent className="py-10 text-center text-muted-foreground">
          No portfolio data yet — sync your account to get started.
        </CardContent>
      </Card>
    )
  }

  // Holdings plus uninvested cash: what the account is actually worth. `cashIsTracked`
  // refuses the two states that would put a confident 0.00 cash figure here instead.
  const hasCash = summary ? cashIsTracked(summary) : false
  const cash = summary?.total_cash_eur ?? 0
  const headline = summary
    ? hasCash
      ? (summary.total_value_eur ?? summary.total_market_value_eur + cash)
      : summary.total_market_value_eur
    : null
  // Holdings the backend could not value today: their cost still counts, so the total is
  // a partial sum while cost basis is whole.
  const unpriced = summary?.unpriced_holdings ?? 0

  return (
    <Card className="overflow-hidden">
      <div className="p-[var(--card-padding)] pb-2 sm:pb-2">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0">
            <p className="text-sm font-medium text-muted-foreground">
              {hasCash || !summary ? 'Total value' : 'Market value'}
            </p>
            {headline === null ? (
              <div className="mt-2 h-12 w-64 animate-pulse rounded-lg bg-muted" />
            ) : (
              // The one hero figure on the page: 48px from `sm` up, proportional digits.
              <p className="mt-1 text-[2.5rem] font-semibold leading-none tracking-tight sm:text-5xl">
                <HeroAmount value={headline} currency={baseCurrency} />
              </p>
            )}
            {/* Absent rather than `+0.00%` while the range is unknown or held nothing. */}
            {period && (
              <p className="mt-3 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
                <span
                  className={cn(
                    'inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 font-semibold',
                    period.periodGain >= 0 ? GAIN_PILL : LOSS_PILL,
                  )}
                >
                  <span aria-hidden="true" className="text-[0.7em]">{period.periodGain >= 0 ? '▲' : '▼'}</span>
                  <span>{signed(period.periodGain)}</span>
                  {period.periodGainPercent !== null && (
                    <>
                      <span aria-hidden="true" className="opacity-50">·</span>
                      <span>{signedPct(period.periodGainPercent)}</span>
                    </>
                  )}
                </span>
                <span className="text-muted-foreground">{RANGE_LABEL[range]}</span>
              </p>
            )}
          </div>
          <div className="min-w-0">{controls}</div>
        </div>

        {unpriced > 0 && (
          <div
            role="alert"
            className="mt-4 rounded-lg border border-yellow-600/40 bg-yellow-600/10 px-3 py-2 text-xs text-yellow-700 dark:text-yellow-500"
          >
            <span className="font-medium">
              {hasCash ? 'Total value' : 'Market value'} is incomplete — {unpriced}{' '}
              {unpriced === 1 ? 'holding has' : 'holdings have'} no usable price today
            </span>
            , so the total and both gains understate.
          </div>
        )}
      </div>

      <div className="px-2 pb-2 sm:px-4">{children}</div>

      {summary && (
        // Two columns until there is room for all of them in one row; an odd last cell
        // spans the pair rather than leaving a blank tile beside it.
        <dl
          className={cn(
            'grid grid-cols-2 gap-px border-t border-border/70 bg-border/60 [&>div]:bg-card',
            '[&>div:last-child:nth-child(odd)]:col-span-2 md:[&>div:last-child:nth-child(odd)]:col-span-1',
            hasCash ? 'md:grid-cols-5' : 'md:grid-cols-4',
          )}
        >
          <Stat label="Cost basis" value={formatCurrency(summary.total_cost_basis_eur)} />
          <Stat
            label="Unrealized gain"
            value={signed(summary.total_gain_loss_eur)}
            tone={summary.total_gain_loss_eur >= 0 ? GAIN : LOSS}
            sub={signedPct(summary.total_gain_loss_percent)}
          />
          <Stat
            label="Realized gain"
            value={signed(summary.total_realized_gain_loss_eur)}
            tone={summary.total_realized_gain_loss_eur >= 0 ? GAIN : LOSS}
          />
          {hasCash && <Stat label="Cash" value={formatCurrency(cash)} />}
          <Stat label="Positions" value={String(summary.num_positions)} />
        </dl>
      )}
    </Card>
  )
}

function Stat({ label, value, tone, sub }: { label: string; value: string; tone?: string; sub?: string }) {
  return (
    <div className="px-[var(--card-padding)] py-3.5 sm:py-4">
      <dt className="text-xs font-medium text-muted-foreground">{label}</dt>
      <dd className="mt-1 flex flex-wrap items-baseline gap-x-1.5 text-base font-semibold tracking-tight sm:text-lg">
        <span className={tone}>{value}</span>
        {sub && <span className={cn('text-xs font-medium', tone)}>{sub}</span>}
      </dd>
    </div>
  )
}
