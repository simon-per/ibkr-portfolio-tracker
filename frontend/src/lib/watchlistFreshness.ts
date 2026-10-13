/**
 * How old the watchlist's data is, for the as-of line beside the tab's title.
 *
 * The watchlist is refreshed by its own scheduled job (04:00 Europe/Berlin, Tuesday to
 * Saturday — `WATCHLIST_REFRESH_HOUR` in `scheduler_service.py`) and by *Refresh All*.
 * Until 2026-10-13 nothing scheduled it and nothing on the page said when it had last
 * run, so seven-week-old prices and scores read as today's.
 */

/**
 * Past this the as-of line turns amber. Four days, because the schedule's own longest
 * gap is three — Saturday's run to Tuesday's — and a marker that lights up every
 * ordinary weekend teaches the reader to skip it.
 */
export const WATCHLIST_STALE_AFTER_DAYS = 4

/** Rows synced more than this far apart are reported as a range, not one time. */
const MIXED_AFTER_MS = 24 * 60 * 60 * 1000

export interface WatchlistFreshness {
  /** The oldest sync among the rows that have one. The headline: it is the honest one. */
  oldest: Date | null
  newest: Date | null
  /** Rows added while a sync was busy or failing — they carry no data at all yet. */
  neverSynced: number
  /** The rows were synced more than a day apart, so one timestamp would misdescribe some. */
  mixed: boolean
  stale: boolean
}

/** `null` for an empty watchlist: there is nothing whose age could be stated. */
export function watchlistFreshness(
  items: { last_synced: string | null }[],
  now: Date,
): WatchlistFreshness | null {
  if (items.length === 0) return null

  const times = items
    .map(i => (i.last_synced ? new Date(i.last_synced).getTime() : NaN))
    .filter(t => !Number.isNaN(t))
  const neverSynced = items.length - times.length
  if (times.length === 0) {
    return { oldest: null, newest: null, neverSynced, mixed: false, stale: true }
  }

  const oldest = Math.min(...times)
  const newest = Math.max(...times)
  const staleAfterMs = WATCHLIST_STALE_AFTER_DAYS * 24 * 60 * 60 * 1000
  return {
    oldest: new Date(oldest),
    newest: new Date(newest),
    neverSynced,
    mixed: newest - oldest > MIXED_AFTER_MS,
    stale: neverSynced > 0 || now.getTime() - oldest > staleAfterMs,
  }
}
