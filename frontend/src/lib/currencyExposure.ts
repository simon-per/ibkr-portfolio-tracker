/**
 * Which currency the portfolio is *quoted* in, position by position.
 *
 * The distinction in that sentence is the whole design. `securities.currency` is
 * the currency a listing trades in, which for a direct holding is also its
 * economic exposure — but for a fund it need not be, and on this account it
 * frequently is not: SXR8 is a EUR-listed S&P 500 tracker, so its quote currency
 * is EUR and its risk is almost entirely USD. A breakdown that folded it into the
 * EUR bucket would be confidently backwards on exactly the positions where an
 * investor asks the question.
 *
 * Nothing here infers the missing half. The ETF look-through table
 * (`app/etf_mappings.py`) maps regions, not currencies, and regions do not
 * determine currency — "Europe" spans EUR, GBP, CHF and SEK, "Asia Pacific"
 * spans JPY, AUD, HKD and TWD. Re-attributing on that basis would be invention.
 * So funds are reported *alongside* the split with their share named, and the
 * caller states the limit rather than papering over it.
 *
 * Two further rules, both the same shape as the rebalance panel's:
 *
 * - **An unpriced position is excluded and counted**, never folded in at 0. The
 *   portfolio values a holding with no cached price at 0.00, so including it
 *   would understate its currency by exactly that position.
 * - **A missing currency is its own bucket**, not dropped and not assumed to be
 *   the base currency.
 */

import { isUnpriced } from './positionValuation'

export interface CurrencyExposureInput {
  security_id: number
  symbol: string
  /** The listing's quote currency. Blank or absent lands in `UNKNOWN_CURRENCY`. */
  currency: string | null
  /** Already projected into the base currency by the API. */
  market_value_eur: number
  market_price: number | null
}

export const UNKNOWN_CURRENCY = 'Unknown'

export interface CurrencyRow {
  currency: string
  marketValue: number
  pct: number
  positionCount: number
  /** Of the above, those that are funds — where the quote currency may mislead. */
  fundCount: number
  fundValue: number
}

export interface CurrencyExposure {
  rows: CurrencyRow[]
  totalValue: number
  /** Positions the portfolio cannot value, so they carry no currency weight. */
  unpricedCount: number
  /** Share of the book in funds whose quote currency need not be their exposure. */
  lookThroughValue: number
  lookThroughPct: number
  lookThroughCount: number
}

// `isUnpriced` is shared — see `positionValuation.ts`. What it means here specifically: a
// price that resolves while its FX rate does not leaves `market_value_eur` at 0 with
// `market_price` still set, which would count the holding as a real member of its currency
// bucket while contributing nothing to it, and keep it out of `unpricedCount` where the
// caveat is stated.

/**
 * `fundSymbols` names the positions whose quote currency should not be read as
 * exposure — in practice the ETF bucket of `/api/allocation/portfolio`, which the
 * Allocation tab already holds.
 *
 * Matching is by symbol because that is what the allocation response carries.
 * Security identity here is `isin + exchange`, so a symbol is not unique; the
 * collision that would matter is a *stock* sharing a held fund's ticker, which
 * would be flagged as a fund. Accepted — the alternative is not flagging funds at
 * all, and the flag is a caveat rather than a figure.
 */
export function computeCurrencyExposure(
  positions: CurrencyExposureInput[],
  fundSymbols: ReadonlySet<string> = new Set(),
): CurrencyExposure {
  const priced = positions.filter((p) => !isUnpriced(p))
  const totalValue = priced.reduce((s, p) => s + p.market_value_eur, 0)

  const byCurrency = new Map<string, CurrencyRow>()
  let lookThroughValue = 0
  let lookThroughCount = 0

  for (const p of priced) {
    const currency = p.currency?.trim() ? p.currency.trim().toUpperCase() : UNKNOWN_CURRENCY
    let row = byCurrency.get(currency)
    if (!row) {
      row = {
        currency,
        marketValue: 0,
        pct: 0,
        positionCount: 0,
        fundCount: 0,
        fundValue: 0,
      }
      byCurrency.set(currency, row)
    }
    row.marketValue += p.market_value_eur
    row.positionCount += 1

    if (fundSymbols.has(p.symbol)) {
      row.fundCount += 1
      row.fundValue += p.market_value_eur
      lookThroughValue += p.market_value_eur
      lookThroughCount += 1
    }
  }

  const rows = [...byCurrency.values()].sort((a, b) => b.marketValue - a.marketValue)
  for (const row of rows) {
    row.pct = totalValue > 0 ? (row.marketValue / totalValue) * 100 : 0
  }

  return {
    rows,
    totalValue,
    unpricedCount: positions.length - priced.length,
    lookThroughValue,
    lookThroughPct: totalValue > 0 ? (lookThroughValue / totalValue) * 100 : 0,
    lookThroughCount,
  }
}

/**
 * How concentrated the quote-currency mix is, as the share sitting outside the
 * base currency.
 *
 * The complement of "home currency" weight: for a CHF-based investor this is the
 * proportion of the book whose quoted value moves with an exchange rate before
 * anything else happens. Null when nothing is priced, rather than 0 — no
 * positions is an unknown exposure, not an unhedged-free one.
 */
export function foreignQuotedPct(
  exposure: CurrencyExposure,
  baseCurrency: string,
): number | null {
  if (exposure.totalValue <= 0) return null
  const base = baseCurrency.trim().toUpperCase()
  const home = exposure.rows.find((r) => r.currency === base)
  return 100 - (home ? home.pct : 0)
}
