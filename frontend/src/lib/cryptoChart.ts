import type { CryptoHistoryPoint, CryptoPortfolioResponse } from './api'
import { toLocalISODate } from './dateRanges'
import { hasIdentity, OTHER_COLOR, seriesColor } from './dividendColors'

/**
 * The crypto view's chart arithmetic, kept out of the components so it is testable in node.
 */

export type CryptoRange = '1M' | '3M' | '6M' | '1Y' | 'ALL'
export const CRYPTO_RANGES: readonly CryptoRange[] = ['1M', '3M', '6M', '1Y', 'ALL']

const RANGE_DAYS: Record<Exclude<CryptoRange, 'ALL'>, number> = {
  '1M': 30,
  '3M': 91,
  '6M': 182,
  '1Y': 365,
}

/**
 * The points a range button shows, sliced from the one series `/history` serves.
 *
 * ALL is the whole series — not `rangeFor('ALL')`, which caps at five years because the
 * stock timeline endpoint refuses wider spans; CoinStats' history has no such limit and a
 * crypto book can well be older. The cutoff is local calendar arithmetic, never
 * `toISOString`, for the reason `lib/dateRanges.ts` gives.
 */
export function sliceRange(
  points: readonly CryptoHistoryPoint[],
  range: CryptoRange,
  today: Date = new Date(),
): CryptoHistoryPoint[] {
  if (range === 'ALL') return [...points]
  const cutoff = toLocalISODate(
    new Date(today.getFullYear(), today.getMonth(), today.getDate() - RANGE_DAYS[range]),
  )
  return points.filter(point => point.date >= cutoff)
}

/**
 * The value line's points: the days CoinStats sampled a value on.
 *
 * `/portfolio/chart?type=all` samples the value **every three days** (measured 2026-09-28)
 * while the P&L history is daily, and `/history` merges the two by date — so two days in
 * three carry no value. That is sampling, not a missing measurement: the line is drawn
 * through the samples, as CoinStats' own chart is. The P&L line does not get this
 * treatment; there a missing day is a real gap (`rangePnlSeries`).
 */
export function valueSeries(points: readonly CryptoHistoryPoint[]): CryptoHistoryPoint[] {
  return points.filter(point => point.value != null)
}

/**
 * The P&L line over a range: CoinStats' daily P&L, summed from 0 on the range's first point.
 *
 * `/portfolio/pl/history` serves **one day's** cash-flow-adjusted P&L per point, not a
 * running total — measured 2026-09-28 (consecutive points differ by about their own size).
 * Drawn raw, that is noise around zero; summed, it is what the account gained or lost over
 * the range with deposits and withdrawals left out. Starting at 0 on the first point is the
 * rule the stock chart's Profit/Loss line follows too: the first point is the end of that
 * day, so its own P&L is before the range.
 *
 * **An unknown day makes every later point unknown.** A sum that skipped it would treat
 * that day's P&L as 0 and draw the result as if it were complete. The newest point — the
 * live snapshot, which has no daily P&L — can only ever end the line.
 */
export function rangePnlSeries(points: readonly CryptoHistoryPoint[]): (number | null)[] {
  let running: number | null = 0
  return points.map((point, i) => {
    if (i === 0) return 0
    running = running == null || point.pnl == null ? null : running + point.pnl
    return running
  })
}

export interface AllocationSlice {
  key: string
  label: string
  value: number
  /** Share of the portfolio total — the backend's `weight_pct` for a coin. */
  pct: number | null
  color: string
  kind: 'coin' | 'other' | 'unitemised'
}

/** Coins drawn as themselves in the donut; the rest fold into Other. */
export const DONUT_COINS = 7

export const UNITEMISED_COLOR = 'var(--viz-unattributed)'

/**
 * The donut's slices: the largest coins that have a colour identity, then Other, then
 * the part of the total CoinStats does not itemise.
 *
 * **Shares of the whole total, never of the itemised part.** The "Not itemised" slice is
 * what keeps the donut honest: without it the coins would fill the circle and every slice
 * would overstate its share — the renormalisation this codebase refuses everywhere else.
 *
 * Colour is by identity (`color_order`, CoinStats' market-cap rank), so a coin keeps its
 * colour when another overtakes it by value. A large coin whose rank falls outside the
 * palette's identities folds into Other rather than being drawn in Other's grey beside it.
 */
export function allocationSlices(portfolio: CryptoPortfolioResponse): AllocationSlice[] {
  const total = portfolio.total_value
  const share = (value: number) => (total && total > 0 ? (value / total) * 100 : null)

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
  const unitemised = portfolio.unitemised_value
  if (unitemised != null && unitemised > 0) {
    slices.push({
      key: '__unitemised', label: 'Not itemised', value: unitemised, pct: share(unitemised),
      color: UNITEMISED_COLOR, kind: 'unitemised',
    })
  }
  return slices
}
