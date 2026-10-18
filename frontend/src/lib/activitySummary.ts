import type { ActivityKind, ActivityResponse, ActivityRow } from './api'

/**
 * The Activity tab's figures strip: what the filtered window adds up to.
 *
 * Summed from the ledger rows themselves, so it can only be as complete as the rows in
 * hand. The endpoint pages, and a sum over one page of a longer window is a number that
 * looks like the window's and is not — so **a figure is `null` unless every row of the
 * window is present** (`items.length === total`), never a partial sum. Within a complete
 * window a figure is still `null` when any row it would add has no amount (a missing FX
 * rate), and when its kind is filtered out: a filtered-out kind did not sum to zero.
 */

/**
 * The sale side, as the server decides it (`is_sale`): a sale or the cancellation of one;
 * every other trade is the buy side. A cancellation carries its execution's amounts
 * negated, so summing it on the same side as its sale nets a busted sale out of `sold`
 * and `realized` (and a busted buy out of `bought`). This read the subtype until issue
 * #58, a second copy of a rule the server applies — and the two disagreed.
 */
export function isSell(row: ActivityRow): boolean {
  return row.kind === 'trade' && row.is_sale === true
}

export interface ActivitySummary {
  /** Events in the window, from the response's own count — known even when paged. */
  events: number
  /** Signed: money out is negative, as in the ledger's Amount column. */
  bought: number | null
  sold: number | null
  realized: number | null
  dividends: number | null
  /** True when an estimated (pre-IBKR) dividend is part of `dividends`. */
  dividendsEstimated: boolean
  /** Deposits less withdrawals; a transfer is never money in (whitelisted by the server). */
  netDeposits: number | null
}

/** Sum `pick` over `rows`; null when any picked value is unknown. An empty set sums to 0. */
function sumKnown(rows: readonly ActivityRow[], pick: (row: ActivityRow) => number | null): number | null {
  let total = 0
  for (const row of rows) {
    const value = pick(row)
    if (value == null || !Number.isFinite(value)) return null
    total += value
  }
  return total
}

export function summarizeActivity(
  response: ActivityResponse,
  /** The kind filter; empty means every kind. */
  kinds: readonly ActivityKind[],
): ActivitySummary {
  const complete = response.offset === 0 && response.items.length === response.total
  const shown = (kind: ActivityKind) => complete && (kinds.length === 0 || kinds.includes(kind))
  const rows = response.items
  const trades = rows.filter(r => r.kind === 'trade')
  const sells = trades.filter(isSell)
  const buys = trades.filter(r => !isSell(r))
  const dividends = rows.filter(r => r.kind === 'dividend')
  const moneyIn = rows.filter(r => r.kind === 'cash_flow' && r.counts_as_money_in === true)
  const amount = (r: ActivityRow) => r.amount_base

  return {
    events: response.total,
    bought: shown('trade') ? sumKnown(buys, amount) : null,
    sold: shown('trade') ? sumKnown(sells, amount) : null,
    realized: shown('trade') ? sumKnown(sells, r => r.realized_pnl_base) : null,
    dividends: shown('dividend') ? sumKnown(dividends, amount) : null,
    dividendsEstimated: shown('dividend') && dividends.some(r => r.source === 'yfinance_estimate'),
    netDeposits: shown('cash_flow') ? sumKnown(moneyIn, amount) : null,
  }
}
