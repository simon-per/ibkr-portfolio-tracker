import type { CompanyDividendTrack, DividendSecurityRow } from './api'
import { compareNullsLast } from './nullsLast'

/**
 * Sorting for the per-position dividend table.
 *
 * `DataTable` owns no sort state (docs/frontend.md), so the comparator lives with the
 * table that needs it, the way Fundamentals, Watchlist and the crypto holdings keep
 * theirs. Two rules:
 *
 * - **An unknown sorts last in both directions.** A dash is not a zero — a payer with no
 *   payment a year back has no growth, not the lowest growth — so flipping the direction
 *   must not lift every dash to the top.
 * - **A key sorts what the cell shows.** With the Forecast toggle off the Forecast and
 *   Next cells print a dash, so they sort as unknown rather than by a figure nobody can
 *   see.
 *
 * Ties fall back to the symbol, then the security, so equal rows cannot swap between
 * renders.
 */
export type DividendSortKey =
  | 'symbol' | 'net' | 'yield' | 'payouts' | 'share' | 'forecast' | 'next'
  | 'growth' | 'yoc' | 'dpsCagr' | 'streak' | 'payout'

/** Each held listing's company dividend record, by security id (`/dividend-track`). */
export type DividendTrackMap = Readonly<Record<string, CompanyDividendTrack>>

export type DividendSortDirection = 'asc' | 'desc'

/** The server's own order is received + projected, biggest first — which is Share. */
export const DEFAULT_DIVIDEND_SORT: { column: DividendSortKey; direction: DividendSortDirection } = {
  column: 'share',
  direction: 'desc',
}

function sortValue(
  row: DividendSecurityRow,
  key: DividendSortKey,
  showForecast: boolean,
  track: DividendTrackMap | undefined,
): number | string | null {
  const own = track?.[String(row.security_id)]
  switch (key) {
    case 'symbol': return row.symbol
    case 'net': return row.net_eur
    case 'yield': return row.forward_yield_pct
    case 'payouts': return row.payouts
    case 'share': return row.share_pct
    case 'forecast': return showForecast && row.forecast_net_eur > 0 ? row.forecast_net_eur : null
    case 'next': return showForecast ? row.next_pay_date : null
    case 'growth': return row.per_share_growth_pct ?? null
    case 'yoc': return row.yield_on_cost_pct
    case 'dpsCagr': return own?.dps_cagr_5y ?? null
    case 'streak': return own?.raise_years ?? null
    case 'payout': return own?.payout_ratio ?? null
  }
}

export function sortDividendRows(
  rows: readonly DividendSecurityRow[],
  column: DividendSortKey,
  direction: DividendSortDirection,
  showForecast: boolean,
  track?: DividendTrackMap,
): DividendSecurityRow[] {
  const value = (row: DividendSecurityRow) => sortValue(row, column, showForecast, track)
  return [...rows].sort((a, b) =>
    compareNullsLast(value(a), value(b), direction)
      || a.symbol.localeCompare(b.symbol) || a.security_id - b.security_id,
  )
}

/** A new column starts in the direction a reader wants first: names A–Z, figures biggest. */
export function initialDirection(column: DividendSortKey): DividendSortDirection {
  return column === 'symbol' || column === 'next' ? 'asc' : 'desc'
}
