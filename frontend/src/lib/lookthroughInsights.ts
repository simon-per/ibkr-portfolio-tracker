import type { LookthroughCompanyRow, LookthroughResponse } from './api'

/**
 * The look-through tab's insight strip, as pure data: concentration figures read off the company
 * rows already loaded, so the strip costs no request and has no arithmetic of its own to drift.
 *
 * **Every percentage is the row's own `pct_of_portfolio` — a share of the whole book — and a sum
 * of those is never divided by anything.** The covered part is ~79% of the book; a "top 10"
 * taken over the covered part would inflate every figure by the undecomposed remainder, which
 * is the renormalisation this feature refuses everywhere else.
 *
 * Deliberately **no sector figure**. The response carries a sector per company for grouping the
 * treemap only, and a "largest sector" headline would be the portfolio-level rollup
 * docs/lookthrough.md declines — a second sector answer beside the Allocation tab's, over a
 * different denominator.
 *
 * The rows are the response's top N (25/50/100). The largest exposure and the top 10 are decided
 * inside the top 25, so the limit control never moves them; the other two read the loaded rows and
 * can change when the limit is raised (a small direct holding joins *Held both ways*).
 */

export interface CompanyHighlight {
  companyKey: string
  name: string
  pct: number
  value: number
  isins: string[]
  listings: string[]
}

export interface LookthroughInsights {
  /** The single largest company exposure. */
  largest: CompanyHighlight | null
  /** The `count` largest companies' combined share of the book; count < `n` when fewer exist. */
  top: { count: number; pct: number } | null
  /** The largest company reached only through funds — the exposure nobody chose by name. */
  largestViaFunds: CompanyHighlight | null
  /** Companies held directly AND through funds, and what the funds add on top of the direct line. */
  heldBothWays: { count: number; viaFundsValue: number } | null
}

const NONE: LookthroughInsights = { largest: null, top: null, largestViaFunds: null, heldBothWays: null }

function highlight(row: LookthroughCompanyRow): CompanyHighlight {
  return {
    companyKey: row.company_key,
    name: row.name,
    pct: row.pct_of_portfolio,
    value: row.value_eur,
    isins: row.isins,
    listings: row.listings,
  }
}

/**
 * Returns all-absent when there is no total to take a share of, or no company to rank — the
 * `concentrationPct` refusal: an unpriced book gets dashes, not a confident 0%.
 */
export function lookthroughInsights(data: LookthroughResponse, n = 10): LookthroughInsights {
  if (!(data.total_market_value_eur > 0)) return NONE
  // Ranked here rather than trusted: the server sorts, but an order this strip depends on
  // should not be an assumption. Ties on the key, so equal rows cannot swap between renders.
  const rows = data.companies
    .filter((r) => r.value_eur > 0)
    .sort((a, b) => b.value_eur - a.value_eur || a.company_key.localeCompare(b.company_key))
  if (rows.length === 0) return NONE

  const top = rows.slice(0, n)
  const viaFundsOnly = rows.find((r) => !(r.direct_value_eur > 0) && r.via_funds_value_eur > 0)
  const both = rows.filter((r) => r.direct_value_eur > 0 && r.via_funds_value_eur > 0)

  return {
    largest: highlight(rows[0]),
    top: { count: top.length, pct: top.reduce((acc, r) => acc + r.pct_of_portfolio, 0) },
    largestViaFunds: viaFundsOnly ? highlight(viaFundsOnly) : null,
    heldBothWays: {
      count: both.length,
      viaFundsValue: both.reduce((acc, r) => acc + r.via_funds_value_eur, 0),
    },
  }
}
