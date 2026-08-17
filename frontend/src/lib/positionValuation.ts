/**
 * One answer to "could the backend value this position?", for every client-side
 * statistic that weights positions.
 *
 * **Why this is a module and not three functions.** The predicate lived inline in
 * `rebalance.ts` and `currencyExposure.ts` — identical, correct, and each carrying its own
 * copy of the reasoning — and a third consumer (`winRate`) made it three. That is the
 * failure mode CLAUDE.md opens with, and the sharpest form of it: *a correct copy is still
 * a copy*, because agreement is what a copy looks like right up to the moment it stops
 * being one. These two in particular have already had to be corrected together once, on
 * 2026-08-05, when the one-clause form turned out to miss the FX case below.
 *
 * `positionValuation.test.ts` pins the family rather than the instance: no other module
 * may spell this rule out for itself.
 */

/** The minimum a position must expose to be judged. Structural, so each caller keeps its
 *  own richer input type without this module knowing about any of them. */
export interface ValuablePosition {
  market_price: number | null
  market_value_eur: number
}

/**
 * A position the portfolio could not value, for **either** reason.
 *
 * **`market_price === null` alone is not enough.** The backend fails to value a holding two
 * ways and only one of them clears the price: when the price resolves but its **FX rate
 * does not**, `get_positions_breakdown` sets `market_value_eur = 0.0` and leaves
 * `market_price` populated. Such a position slips through as *priced* and takes a 0%
 * weight — so drift advised buying its entire target, a currency bucket counted a member
 * contributing nothing, and Win Rate counted an ordinary-looking loser. A missing FX rate
 * is not hypothetical here: Frankfurter cannot serve TWD at all, which is why
 * `WARM_CURRENCIES` exists.
 *
 * The zero-value clause used to be argued away on the grounds that a *fully-sold* holding
 * is priced and genuinely 0%. That premise was false: `get_positions_breakdown` selects
 * `is_open == True`, so a sold-out security has no open lots and never reaches the client.
 * What a zero value on a priced position actually means is the FX failure above — or a
 * zero-quantity open lot, which is a data anomaly that should equally not drive advice.
 *
 * **Narrower than `summary.unpriced_holdings`**, which is the backend's own count and the
 * one to trust for a headline figure. This exists only to decide whether one row may enter
 * a client-side statistic.
 */
export function isUnpriced(p: ValuablePosition): boolean {
  return p.market_price === null || p.market_value_eur <= 0
}

/** The complement, for callers that read better in the positive. */
export function isValuable(p: ValuablePosition): boolean {
  return !isUnpriced(p)
}
