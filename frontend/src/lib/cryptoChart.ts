import type { CryptoHistoryPoint, CryptoPortfolioResponse } from './api'
import { toLocalISODate } from './dateRanges'
import { hasIdentity, OTHER_COLOR, seriesColor } from './dividendColors'
import { parseLocalDate } from './utils'

/**
 * The crypto view's chart arithmetic, kept out of the components so it is testable in node.
 */

export type CryptoRange = '1W' | 'MTD' | '1M' | '3M' | '6M' | 'YTD' | '1Y' | 'ALL'
export const CRYPTO_RANGES: readonly CryptoRange[] = ['1W', 'MTD', '1M', '3M', '6M', 'YTD', '1Y', 'ALL']

/**
 * The crypto book starts here (`CRYPTO_HISTORY_START` in `crypto_book.py`): earlier data
 * is not reliable even when back-calculated, so no range reaches before it.
 */
export const CRYPTO_START = '2026-01-01'

const RANGE_DAYS: Record<'1W' | '1M' | '3M' | '6M' | '1Y', number> = {
  '1W': 7,
  '1M': 30,
  '3M': 91,
  '6M': 182,
  '1Y': 365,
}

/** The first day a range shows, counted back from the series' latest point. */
export function rangeStart(range: CryptoRange, latest: string): string {
  const end = parseLocalDate(latest)
  let start: string
  if (range === 'ALL') start = CRYPTO_START
  else if (range === 'MTD') start = toLocalISODate(new Date(end.getFullYear(), end.getMonth(), 1))
  else if (range === 'YTD') start = toLocalISODate(new Date(end.getFullYear(), 0, 1))
  else start = toLocalISODate(
    new Date(end.getFullYear(), end.getMonth(), end.getDate() - RANGE_DAYS[range]),
  )
  return start < CRYPTO_START ? CRYPTO_START : start
}

/**
 * The points a range button shows, sliced from the one daily series `/history` serves.
 *
 * Every range ends at the **latest point** — today's live valuation — and counts back from
 * it: 1W is the seven days before it (eight points, seven daily moves), MTD starts on the
 * 1st of its month (one point on the 1st itself), YTD on 1 January of its year. Nothing
 * reaches before `CRYPTO_START`; ALL and 1Y clamp to it. The cutoff is local calendar
 * arithmetic on ISO dates, never `toISOString`, for the reason `lib/dateRanges.ts` gives.
 */
export function sliceRange(
  points: readonly CryptoHistoryPoint[],
  range: CryptoRange,
): CryptoHistoryPoint[] {
  if (points.length === 0) return []
  const cutoff = rangeStart(range, points[points.length - 1].date)
  return points.filter(point => point.date >= cutoff)
}

/**
 * The value line's points. The series is daily, so a day with no value is a day nobody
 * could price — a real gap, drawn as one (`connectNulls` is off), never bridged.
 */
export function valueSeries(points: readonly CryptoHistoryPoint[]): (number | null)[] {
  return points.map(point => point.value)
}

/**
 * The P&L line over a range: the daily P&L, summed from 0 on the range's first point.
 *
 * Each day's `pnl` is yesterday's coins times the day's price move, so a buy, a DCA or a
 * transfer between exchanges is never a gain. Starting at 0 on the first point is the rule
 * the stock chart's Profit/Loss line follows too: the first point is the end of that day,
 * so its own P&L is before the range.
 *
 * **An unknown day makes every later point unknown.** A sum that skipped it would treat
 * that day's P&L as 0 and draw the result as if it were complete.
 */
export function rangePnlSeries(points: readonly CryptoHistoryPoint[]): (number | null)[] {
  let running: number | null = 0
  return points.map((point, i) => {
    if (i === 0) return 0
    running = running == null || point.pnl == null ? null : running + point.pnl
    return running
  })
}

/** The reconstructed days inside a slice — before the first synced holdings day — as the
 * first and last date, for the chart's shading; null when the slice has none. */
export function reconstructedSpan(
  points: readonly CryptoHistoryPoint[],
): { from: string; to: string } | null {
  const days = points.filter(point => point.reconstructed)
  if (days.length === 0) return null
  return { from: days[0].date, to: days[days.length - 1].date }
}

export interface AllocationSlice {
  key: string
  label: string
  value: number
  /** Share of the book's total — the backend's `weight_pct` for a coin. */
  pct: number | null
  color: string
  kind: 'coin' | 'other'
}

/** Coins drawn as themselves in the donut; the rest fold into Other. */
export const DONUT_COINS = 7

/**
 * The donut's slices: the largest coins that have a colour identity, then Other.
 *
 * The total is the sum of the priced coins, so the slices fill the circle by construction.
 * A coin with no price that day is not drawn, and its share is unknown — the backend then
 * serves no total and no weights, and the view says which coin it is.
 *
 * Colour is by identity (`color_order`, CoinStats' market-cap rank), so a coin keeps its
 * colour when another overtakes it by value. A large coin whose rank falls outside the
 * palette's identities folds into Other rather than being drawn in Other's grey beside it.
 */
export function allocationSlices(portfolio: CryptoPortfolioResponse): AllocationSlice[] {
  const valued = portfolio.holdings
    .filter(h => h.status === 'valued' && h.value != null && h.value > 0)
    .sort((a, b) => (b.value ?? 0) - (a.value ?? 0))

  const slices: AllocationSlice[] = []
  let otherValue = 0
  let otherPct: number | null = 0
  for (const holding of valued) {
    const value = holding.value as number
    if (slices.length < DONUT_COINS && hasIdentity(holding.coin_id, portfolio.color_order)) {
      slices.push({
        key: holding.coin_id,
        label: holding.symbol ?? holding.name ?? holding.coin_id,
        value,
        pct: holding.weight_pct,
        color: seriesColor(holding.coin_id, portfolio.color_order),
        kind: 'coin',
      })
    } else {
      otherValue += value
      otherPct = otherPct == null || holding.weight_pct == null ? null : otherPct + holding.weight_pct
    }
  }
  if (otherValue > 0) {
    slices.push({
      key: '__other', label: 'Other', value: otherValue, pct: otherPct, color: OTHER_COLOR,
      kind: 'other',
    })
  }
  return slices
}

function dayBefore(iso: string): string {
  const d = parseLocalDate(iso)
  return toLocalISODate(new Date(d.getFullYear(), d.getMonth(), d.getDate() - 1))
}

/**
 * The sentence under the chart that says which days are not measured holdings:
 * before the earliest basket, that basket at each day's price; between it and the first
 * synced day, the basket rebuilt from CoinStats' transactions. Null when every day is
 * synced. `format` renders a date (the view's `formatDate`).
 */
export function reconstructedCaption(
  start: string,
  basketDate: string | null,
  firstSnapshotDate: string | null,
  format: (iso: string) => string,
): string | null {
  if (!basketDate || !firstSnapshotDate) return null
  const parts: string[] = []
  if (basketDate > start) {
    parts.push(`${format(start)} – ${format(dayBefore(basketDate))}: the ${format(basketDate)} coins at each day's price`)
  }
  if (basketDate < firstSnapshotDate) {
    parts.push(`${format(basketDate)} – ${format(dayBefore(firstSnapshotDate))}: holdings rebuilt from CoinStats' transactions`)
  }
  return parts.length ? `Reconstructed (shaded) — ${parts.join('; ')}.` : null
}
