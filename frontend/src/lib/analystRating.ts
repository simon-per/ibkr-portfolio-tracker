import type { AnalystRatingCounts } from '@/lib/api'

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

/** The five counts, without the label — what the bar and its text are drawn from. */
export type RatingCounts = Pick<AnalystRatingCounts, 'strong_buy' | 'buy' | 'hold' | 'sell' | 'strong_sell'>

/**
 * The five counts in order, Strong Buy to Strong Sell, each with its name and the bar
 * segment's paint. A diverging scale: two hues and a grey midpoint for Hold. The poles
 * carry the most contrast against the surface, so light mode paints them darker and dark
 * mode lighter than the mild steps.
 */
export const RATING_SEGMENTS: { key: keyof RatingCounts; label: string; className: string }[] = [
  { key: 'strong_buy', label: 'Strong Buy', className: 'bg-green-700 dark:bg-green-400' },
  { key: 'buy', label: 'Buy', className: 'bg-green-400 dark:bg-green-700' },
  { key: 'hold', label: 'Hold', className: 'bg-zinc-400 dark:bg-zinc-500' },
  { key: 'sell', label: 'Sell', className: 'bg-red-400 dark:bg-red-700' },
  { key: 'strong_sell', label: 'Strong Sell', className: 'bg-red-700 dark:bg-red-400' },
]

/** "25 Strong Buy · 10 Buy · 3 Hold · 1 Sell": the counts that are not zero, in order. */
export function ratingCountsText(counts: RatingCounts): string {
  return RATING_SEGMENTS.filter((s) => counts[s.key] > 0)
    .map((s) => `${counts[s.key]} ${s.label}`)
    .join(' · ')
}

/**
 * The chip a consensus label wears — a soft tint, since a rating is context for a company,
 * not an alarm. The positions table and the Company tab's overview draw the same chip.
 */
export function ratingBadgeClass(consensus: string | null | undefined): string {
  switch (getRatingScore(consensus)) {
    case 5:
    case 4:
      return 'bg-green-500/10 text-green-700 dark:text-green-400'
    case 3:
      return 'bg-yellow-500/10 text-yellow-700 dark:text-yellow-400'
    case 2:
    case 1:
      return 'bg-red-500/10 text-red-700 dark:text-red-400'
    default:
      return 'bg-muted text-muted-foreground'
  }
}
