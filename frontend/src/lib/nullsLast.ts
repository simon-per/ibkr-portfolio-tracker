/**
 * One order for a sortable column with missing values: missing last, whichever way.
 *
 * Four tables wrote this themselves — Fundamentals, the Watchlist, the crypto holdings and
 * the Dividends table — and had drifted. Two tested `=== null`, so a field absent from a
 * response (`undefined`) sorted as if it were a value; two tested `== null`. Strings
 * compared by code unit in three and by `localeCompare` in one, so "Nestlé" and "nestle"
 * landed in different places on different tabs. Here a missing value is `null` or
 * `undefined`, strings compare with `localeCompare`, and a tie is 0 — each table keeps
 * its own tie-break (by symbol, by id) after it. `nullsLast.test.ts` fails a component
 * that rolls the comparison again.
 */
export type SortDirection = 'asc' | 'desc'

export function compareNullsLast(
  a: number | string | null | undefined,
  b: number | string | null | undefined,
  direction: SortDirection,
): number {
  const aMissing = a == null
  const bMissing = b == null
  if (aMissing || bMissing) {
    if (aMissing && bMissing) return 0
    return aMissing ? 1 : -1
  }
  const order =
    typeof a === 'string' && typeof b === 'string'
      ? a.localeCompare(b)
      : a === b ? 0 : a! < b! ? -1 : 1
  if (order === 0) return 0
  return direction === 'asc' ? order : -order
}
