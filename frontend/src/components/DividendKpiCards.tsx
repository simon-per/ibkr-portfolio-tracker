import { useCurrencySymbol, useFormatCurrency } from '@/lib/CurrencyContext'
import { DeltaChip } from './DeltaChip'
import type { DividendGrowth } from '@/lib/api'

interface DividendKpiCardsProps {
  growth: DividendGrowth | null | undefined
  ibkrFrom: string | null
  isLoading?: boolean
}

/**
 * Cents matter here and whole units do not, until the figure gets large.
 *
 * This portfolio earns single-digit amounts a month, so rounding 47.50 to "48"
 * throws away a meaningful part of the number. Above 1000 the cents are the
 * noise instead, and the tooltip carries the exact figure either way.
 */
function amount(value: number): string {
  const decimals = Math.abs(value) < 1000 ? 2 : 0
  return value.toLocaleString('en-US', {
    minimumFractionDigits: decimals, maximumFractionDigits: decimals,
  })
}

function Tile({
  label, value, sub, children, title,
}: {
  label: string
  value: string
  sub?: string
  children?: React.ReactNode
  title?: string
}) {
  return (
    <div className="rounded-lg border border-border bg-card/50 p-4" title={title}>
      <div className="text-xs font-medium text-muted-foreground">{label}</div>
      <div className="mt-1 text-2xl font-semibold leading-none tracking-tight tabular-nums">
        {value}
      </div>
      {/* Wraps: in the 2-up mobile grid a tile is ~155px of usable width, and a
          chip plus its label can be wider than that. Unwrapped, a footer once
          pushed the whole page into horizontal scroll. */}
      <div className="mt-2 flex min-h-[1.25rem] flex-wrap items-center gap-x-1.5 gap-y-1">
        {children}
        {sub && <span className="text-xs text-muted-foreground">{sub}</span>}
      </div>
    </div>
  )
}

/**
 * The four figures that answer "is this growing", in getquin's reading order.
 *
 * Rolling twelve months leads rather than month-over-month, because dividends
 * here are quarterly: March pays and April does not, so a raw MoM swings by
 * ±90% on cadence alone and would say nothing about the portfolio. The average
 * tile once carried the latest month's MoM/YoY as a footnote; sitting under the
 * average it read as the average's own growth (one large September showed +507%),
 * so it now compares the average with last year's average instead.
 */
export function DividendKpiCards({ growth, ibkrFrom, isLoading }: DividendKpiCardsProps) {
  const curSym = useCurrencySymbol()
  const formatCurrency = useFormatCurrency()

  if (isLoading) {
    return (
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        {[0, 1, 2, 3].map(i => (
          <div key={i} className="h-[104px] animate-pulse rounded-lg bg-muted" />
        ))}
      </div>
    )
  }
  if (!growth) return null

  const { ttm, ytd, avg_month: avgMonth } = growth
  const thisYear = new Date().getFullYear()

  const eraCaveat = growth.ttm_crosses_era && ibkrFrom
    ? `The comparison spans ${ibkrFrom}, where the source changes from Yahoo Finance `
      + `estimates to IBKR actuals — comparable in size, not in provenance.`
    : undefined

  return (
    <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
      <Tile
        label={`${thisYear} so far`}
        value={`${curSym}${amount(ytd.net_eur)}`}
        title={
          `${formatCurrency(ytd.net_eur)} received between 1 January and today.`
          + (ytd.prev_net_eur != null
            ? ` Over the same period last year: ${formatCurrency(ytd.prev_net_eur)}.`
              + ` Compared day-for-day — measuring a part year against a whole one`
              + ` would not be growth.`
            : ' No comparable period last year.')
        }
      >
        <DeltaChip pct={ytd.pct} label={`vs ${thisYear - 1} to date`} />
      </Tile>

      <Tile
        label="Last 12 months"
        sub="365 days through today"
        value={`${curSym}${amount(ttm.net_eur)}`}
        title={
          `${formatCurrency(ttm.net_eur)} received over the last 365 days,`
          + ` against ${formatCurrency(ttm.prev_net_eur ?? 0)} in the 365 before that.`
          + ` A rolling window rather than month-over-month, because a quarterly`
          + ` payer leaves most months at zero.`
          + (eraCaveat ? ` ${eraCaveat}` : '')
        }
      >
        <DeltaChip pct={ttm.pct} label="vs prior 12M" caveat={eraCaveat} />
      </Tile>

      <Tile
        label="Next 12 months"
        value={`${curSym}${amount(growth.next_12m_eur)}`}
        title={
          `${formatCurrency(growth.next_12m_eur)} projected over the next 365 days,`
          + ` inferred from each holding's own payout cadence and recent per-share`
          + ` amounts. Nothing forward-looking is published anywhere, so this is an`
          + ` inference — not an announced schedule.`
        }
      >
        <DeltaChip pct={growth.next_12m_vs_ttm_pct} label="vs last 12M" projected />
      </Tile>

      <Tile
        label="Average per month"
        value={`${curSym}${amount(avgMonth.net_eur)}`}
        title={
          `Last 12 months divided by 12: ${formatCurrency(avgMonth.net_eur)} a month,`
          + ` against ${formatCurrency(avgMonth.prev_net_eur ?? 0)} over the prior year.`
          + ` The growth compares the two averages — the same measure as the 12-month`
          + ` one, since both sides are divided by 12. A single month's change is not`
          + ` shown: on a quarterly schedule it measures which month a payer pays in.`
        }
      >
        <DeltaChip pct={avgMonth.pct} label="vs last year's average" />
      </Tile>
    </div>
  )
}
