import type { DividendSecurityRow } from './api'
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
  | 'symbol' | 'net' | 'yield' | 'fwdYield' | 'payouts' | 'share' | 'forecast' | 'next'
  | 'growth' | 'yoc'

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
): number | string | null {
  switch (key) {
    case 'symbol': return row.symbol
    case 'net': return row.net_eur
    case 'yield': return row.trailing_yield_pct
    case 'fwdYield': return row.forward_yield_pct
    case 'payouts': return row.payouts
    case 'share': return row.share_pct
    case 'forecast': return showForecast && row.forecast_net_eur > 0 ? row.forecast_net_eur : null
    case 'next': return showForecast ? row.next_pay_date : null
    case 'growth': return row.per_share_growth_pct ?? null
    case 'yoc': return row.yield_on_cost_pct
  }
}

export function sortDividendRows(
  rows: readonly DividendSecurityRow[],
  column: DividendSortKey,
  direction: DividendSortDirection,
  showForecast: boolean,
): DividendSecurityRow[] {
  return [...rows].sort((a, b) =>
    compareNullsLast(sortValue(a, column, showForecast), sortValue(b, column, showForecast), direction)
      || a.symbol.localeCompare(b.symbol) || a.security_id - b.security_id,
  )
}

/** A new column starts in the direction a reader wants first: names A–Z, figures biggest. */
export function initialDirection(column: DividendSortKey): DividendSortDirection {
  return column === 'symbol' || column === 'next' ? 'asc' : 'desc'
}
