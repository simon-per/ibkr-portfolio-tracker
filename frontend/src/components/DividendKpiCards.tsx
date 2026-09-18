import { useCurrencySymbol, useFormatCurrency } from '@/lib/CurrencyContext'
import { DeltaChip } from './DeltaChip'
import type { DividendGrowth } from '@/lib/api'

interface DividendKpiCardsProps {
  growth: DividendGrowth | null | undefined
  ibkrFrom: string | null
  isLoading?: boolean
}

const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                     'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

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

/** '2026-07' -> 'Jul 2026'. A raw month key beside formatted money reads as debug output. */
function monthName(key: string): string {
  const [y, m] = key.split('-')
  return `${MONTH_NAMES[Number(m) - 1] ?? m} ${y}`
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
      {/* Wraps: in the 2-up mobile grid a tile is ~155px of usable width, and the
          latest-month footer (month + MoM + YoY) is wider than that. Unwrapped it
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
 * ±90% on cadence alone and would say nothing about the portfolio. MoM survives
 * as a labelled footnote on the latest month.
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

  const { ttm, ytd, avg_month: avgMonth, latest_month: latest } = growth
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
          + ` The growth is the same measure as the 12-month one — both sides are`
          + ` divided by the same 12.`
        }
      >
        {latest ? (
          <span
            className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs text-muted-foreground"
            title={
              `${latest.month}: ${formatCurrency(latest.net_eur)} — the most recent`
              + ` month money actually arrived. Month-over-month is shown for`
              + ` completeness, but on a quarterly schedule it mostly reflects which`
              + ` month a payer happens to pay in.`
            }
          >
            <span>{monthName(latest.month)}</span>
            <DeltaChip pct={latest.mom_pct} label="MoM" />
            <DeltaChip pct={latest.yoy_pct} label="YoY" />
          </span>
        ) : (
          <DeltaChip pct={avgMonth.pct} label="vs prior 12M" />
        )}
      </Tile>
    </div>
  )
}
