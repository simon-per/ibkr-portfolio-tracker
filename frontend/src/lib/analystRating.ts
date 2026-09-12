/**
 * Where a consensus rating sits on the conviction scale, as a number a table can sort by.
 *
 * Two tables rank analyst ratings and they did not agree. `PositionsList` scored the
 * display spelling ("Strong Buy") through a switch of its own, while the watchlist
 * `localeCompare`d the backend's snake_case (`strong_buy`) like any other string — so its
 * descending "Analyst" sort put `strong_sell` first, which is alphabetical order dressed
 * as conviction. One scale, both spellings, one place; `ratingSortFamily.test.tsx` sorts
 * a single fixture through both tables and requires the same order.
 *
 * **Higher is better**, so a descending sort — the default every table here starts a
 * new column on — reads Strong Buy → Strong Sell, the way a descending sort on any
 * other column puts the biggest figure first. Unrated rows score {@link UNRATED_SCORE},
 * below every real rating, so that same default puts them last: the sentinel rule the
 * positions table already states for yield on cost.
 */
const RATING_SCORES: Record<string, number> = {
  strong_buy: 5,
  buy: 4,
  hold: 3,
  sell: 2,
  strong_sell: 1,
}

/** Below every real rating. A spelling this module does not know sorts with the unrated. */
export const UNRATED_SCORE = 0

export function getRatingScore(rating: string | null | undefined): number {
  if (!rating) return UNRATED_SCORE
  // "Strong Buy", "strong buy", "strong-buy" and "strong_buy" are one rating.
  const key = rating.trim().toLowerCase().replace(/[\s-]+/g, '_')
  return RATING_SCORES[key] ?? UNRATED_SCORE
}
