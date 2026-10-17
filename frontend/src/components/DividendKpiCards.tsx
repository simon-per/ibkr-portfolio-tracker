import { useCurrencySymbol, useFormatCurrency } from '@/lib/CurrencyContext'
import { DeltaChip } from './DeltaChip'
import type { DividendGrowth } from '@/lib/api'
import { dividendAmount } from '@/lib/dividendFormat'

interface DividendKpiCardsProps {
  growth: DividendGrowth | null | undefined
  isLoading?: boolean
}

const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                     'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** '2026-07' -> 'Jul', or 'Jul 2026' with the year. */
function monthName(key: string, withYear = false): string {
  const [y, m] = key.split('-')
  const name = MONTH_NAMES[Number(m) - 1] ?? m
  return withYear ? `${name} ${y}` : name
}

/** '2026-07'..'2026-09' -> 'Jul–Sep'. */
function span(start: string, end: string, withYear = false): string {
  return `${monthName(start)}–${monthName(end, withYear)}`
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
    <div className="bg-card p-4 sm:p-5" title={title}>
      <div className="text-xs font-medium text-muted-foreground">{label}</div>
      <div className="mt-1.5 text-lg font-semibold leading-tight tracking-tight sm:text-[1.375rem]">
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
export function DividendKpiCards({ growth, isLoading }: DividendKpiCardsProps) {
  const curSym = useCurrencySymbol()
  const formatCurrency = useFormatCurrency()

  if (isLoading) {
    return (
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        {[0, 1, 2, 3].map(i => (
          <div key={i} className="h-[104px] animate-pulse rounded-xl bg-muted" />
        ))}
      </div>
    )
  }
  if (!growth) return null

  const { ttm, ytd, avg_month: avgMonth, ytd_pace: ytdPace, recent_pace: recent } = growth
  const thisYear = new Date().getFullYear()

  // A comparison needs something to compare against: with no prior period there is
  // no chip and no "against 0.00" in the title, which would read as a measured zero.
  const prior = (value: number | null) => (value != null ? ` · prior: ${formatCurrency(value)}` : '')

  return (
    // One hairline panel, the Performance tab's KPI look (`KpiPanel`): the tiles paint the
    // card colour and the 1px seams between them show the border colour beneath.
    <div className="grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-border/80 bg-border/70 xl:grid-cols-4">
      <Tile
        label={`${thisYear} so far`}
        value={dividendAmount(ytd.net_eur, curSym)}
        title={`Net received this year${prior(ytd.prev_net_eur)}`}
      >
        {ytd.prev_net_eur != null && (
          <DeltaChip pct={ytd.pct} label={`vs ${thisYear - 1} to date`} />
        )}
      </Tile>

      <Tile
        label="Last 12 months"
        value={dividendAmount(ttm.net_eur, curSym)}
        title={`Net received, last 365 days${prior(ttm.prev_net_eur)}`}
      >
        {ttm.prev_net_eur != null && <DeltaChip pct={ttm.pct} label="vs prior 12M" />}
      </Tile>

      <Tile
        label="Next 12 months"
        value={dividendAmount(growth.next_12m_eur, curSym)}
        title="Projected net, next 365 days"
      >
        <DeltaChip pct={growth.next_12m_vs_ttm_pct} label="vs last 12M" projected />
      </Tile>

      <Tile
        label="Average per month"
        value={dividendAmount(avgMonth.net_eur, curSym)}
        title={`Last 12 months ÷ 12${prior(avgMonth.prev_net_eur)}`}
      >
        {ytdPace ? (
          <DeltaChip
            pct={ytdPace.pct}
            label={`${thisYear} vs ${ytdPace.prev_year}`}
            title={
              `${thisYear}: ${formatCurrency(ytdPace.net_eur)}/mo · `
              + `${ytdPace.prev_year}: ${formatCurrency(ytdPace.prev_net_eur)}/mo`
            }
          />
        ) : avgMonth.prev_net_eur != null ? (
          <DeltaChip pct={avgMonth.pct} label="vs prior 12M" />
        ) : null}
        {recent && (
          <DeltaChip
            pct={recent.pct}
            label={`${span(recent.start, recent.end)} vs ${span(recent.prev_start, recent.prev_end)}`}
            title={
              `${span(recent.start, recent.end, true)}: ${formatCurrency(recent.net_eur)}/mo · `
              + `${span(recent.prev_start, recent.prev_end, true)}: ${formatCurrency(recent.prev_net_eur)}/mo`
            }
          />
        )}
      </Tile>
    </div>
  )
}
