import { CalendarCheck, CalendarClock, ChartColumn, Coins } from 'lucide-react'
import { useCurrencySymbol, useFormatCurrency } from '@/lib/CurrencyContext'
import { DeltaChip } from './DeltaChip'
import { KpiCard, KpiCardSkeleton, KpiPanel } from '@/components/ui/KpiCard'
import type { DividendGrowth } from '@/lib/api'
import { FLAT_BAND_PCT } from '@/lib/delta'
import { dividendAmount } from '@/lib/dividendFormat'

/**
 * The two series the forecast history already carries, oldest first, ending on today's
 * live figures (`forecastTrend`, rule 3) — so each sparkline ends on the figure above it.
 */
export interface DividendKpiTrend {
  /** Trailing twelve months received, per day: the "Last 12 months" tile's own history. */
  received: number[]
  /** The next-12-month forecast, per day: the "Next 12 months" tile's own history. */
  forecast: number[]
}

interface DividendKpiCardsProps {
  growth: DividendGrowth | null | undefined
  isLoading?: boolean
  /** Absent until the forecast history loads; a tile then simply has no sparkline. */
  trend?: DividendKpiTrend | null
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

/**
 * The four figures that answer "is this growing", in getquin's reading order.
 *
 * Rolling twelve months leads rather than month-over-month, because dividends
 * here are quarterly: March pays and April does not, so a raw MoM swings by
 * ±90% on cadence alone and would say nothing about the portfolio. The average
 * tile once carried the latest month's MoM/YoY as a footnote; sitting under the
 * average it read as the average's own growth (one large September showed +507%),
 * so it now compares the average with last year's average instead.
 *
 * Growth is the headline (owner, 2026-10-17): each change is a pill beside its figure, and
 * the two twelve-month tiles draw their own history underneath. The sparklines are the
 * forecast history's series, which is unwindowed like every figure here — a line taken from
 * the selected range would move under a figure that does not.
 *
 * Every chip keeps the dividend flat band: income is lumpy, and a KPI pill that went green
 * on +3% would disagree with the same change printed anywhere else on the tab.
 */
export function DividendKpiCards({ growth, isLoading, trend }: DividendKpiCardsProps) {
  const curSym = useCurrencySymbol()
  const formatCurrency = useFormatCurrency()

  if (isLoading) {
    return (
      <KpiPanel columns={4}>
        <KpiCardSkeleton count={4} tile />
      </KpiPanel>
    )
  }
  if (!growth) return null

  const { ttm, ytd, avg_month: avgMonth, ytd_pace: ytdPace, recent_pace: recent } = growth
  const thisYear = new Date().getFullYear()

  // A comparison needs something to compare against: with no prior period there is
  // no chip and no "against 0.00" in the title, which would read as a measured zero.
  const prior = (value: number | null) => (value != null ? ` · prior: ${formatCurrency(value)}` : '')

  const averageDelta = ytdPace
    ? {
        pct: ytdPace.pct,
        label: `${thisYear} vs ${ytdPace.prev_year}`,
        title:
          `${thisYear}: ${formatCurrency(ytdPace.net_eur)}/mo · `
          + `${ytdPace.prev_year}: ${formatCurrency(ytdPace.prev_net_eur)}/mo`,
      }
    : avgMonth.prev_net_eur != null
      ? { pct: avgMonth.pct, label: 'vs prior 12M' }
      : undefined

  return (
    <KpiPanel columns={4}>
      <KpiCard
        tile
        icon={CalendarCheck}
        label={`${thisYear} so far`}
        value={dividendAmount(ytd.net_eur, curSym)}
        title={`Net received this year${prior(ytd.prev_net_eur)}`}
        delta={ytd.prev_net_eur != null
          ? { pct: ytd.pct, label: `vs ${thisYear - 1} to date`, flatBand: FLAT_BAND_PCT }
          : undefined}
      />

      <KpiCard
        tile
        icon={Coins}
        label="Last 12 months"
        value={dividendAmount(ttm.net_eur, curSym)}
        title={`Net received, last 365 days${prior(ttm.prev_net_eur)}`}
        delta={ttm.prev_net_eur != null
          ? { pct: ttm.pct, label: 'vs prior 12M', flatBand: FLAT_BAND_PCT }
          : undefined}
        sparkline={trend?.received}
      />

      <KpiCard
        tile
        icon={CalendarClock}
        label="Next 12 months"
        value={dividendAmount(growth.next_12m_eur, curSym)}
        title="Projected net, next 365 days"
        delta={{
          pct: growth.next_12m_vs_ttm_pct, label: 'vs last 12M', projected: true,
          flatBand: FLAT_BAND_PCT,
        }}
        sparkline={trend?.forecast}
      />

      <KpiCard
        tile
        icon={ChartColumn}
        label="Average per month"
        value={dividendAmount(avgMonth.net_eur, curSym)}
        title={`Last 12 months ÷ 12${prior(avgMonth.prev_net_eur)}`}
        delta={averageDelta && { ...averageDelta, flatBand: FLAT_BAND_PCT }}
        footer={recent && (
          // Wraps: in the 2-up phone grid a tile is ~155px of usable width, and a chip
          // plus its label can be wider than that.
          <div className="flex flex-wrap items-center">
            <DeltaChip
              variant="pill"
              pct={recent.pct}
              label={`${span(recent.start, recent.end)} vs ${span(recent.prev_start, recent.prev_end)}`}
              title={
                `${span(recent.start, recent.end, true)}: ${formatCurrency(recent.net_eur)}/mo · `
                + `${span(recent.prev_start, recent.prev_end, true)}: ${formatCurrency(recent.prev_net_eur)}/mo`
              }
            />
          </div>
        )}
      />
    </KpiPanel>
  )
}
