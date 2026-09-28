/**
 * A holding's colour, decided by the holding and never by where it ranks in the view.
 *
 * It used to be `palette[stackSymbols.indexOf(symbol)]`, where `stackSymbols` was the top
 * eight of the **selected range** — so switching from All time to 2025 changed which eight
 * symbols held slots, every symbol after the first difference shifted one, and most of the
 * chart repainted. Measured on this book: All time and 2025 share four of eight symbols, and
 * GOOGL moved from the third slot to the sixth. The calendar took the same function, so in a
 * past-year view every upcoming payment went grey.
 *
 * The input is now `stack_order` — every symbol the book has ever been paid by, biggest
 * first, computed once over the whole history and identical in every range (see
 * `dividend_service.get_dividend_breakdown`). This is the rule `sectorColors.ts` and
 * `benchmarkColors.ts` already state: colour follows the entity, never its rank.
 *
 * The hexes live in `index.css` as `--viz-series-*`, with the measurements behind them; this
 * module owns only the assignment, so there is one place that decides "the third-biggest
 * payer is slot 3" and one place that decides what slot 3 looks like in each theme. That
 * also means no theme is read in JavaScript: a `var()` fill repaints itself.
 *
 * The order does move as the book grows — a holding overtaking another swaps two slots
 * between sessions. That is the same caveat `benchmarkColors.ts` carries, and the only
 * alternative that never moves (ordering by first payment date) hands the hues to the oldest
 * payers rather than the biggest, which is not what a reader is looking for.
 */

/** Ranks 1..8 get a hue of their own. */
export const SERIES_HUES = 8
/**
 * Ranks 9..13 get a lightness step instead.
 *
 * Five, not seven, and that is measured rather than chosen: several hues collapse toward a
 * neutral under CVD (`--viz-sector-3` becomes L*64 C*4 under deuteranopia), which forbids the
 * lightness bands they land in, and a fill needs 3:1 against its own card to be visible at
 * all. What is left fits five steps >= ΔE 6.2 apart. Seven would have to be spaced ~4.5,
 * below what the ramp is for.
 */
export const SERIES_MUTED = 5
/** Ranks 1..13 are drawn as themselves. Everything after folds into Other. */
export const SERIES_IDENTITIES = SERIES_HUES + SERIES_MUTED

const HUES = Array.from({ length: SERIES_HUES }, (_, i) => `var(--viz-series-${i + 1})`)
const MUTED = Array.from({ length: SERIES_MUTED }, (_, i) => `var(--viz-series-muted-${i + 1})`)

export const OTHER_COLOR = 'var(--viz-series-other)'

/**
 * A key's colour, by its position in an identity order the server computed.
 *
 * Generalised from `dividendColor` on 2026-09-28 for the crypto donut, whose order is
 * `color_order` — valued coins by CoinStats' market-cap rank, which belongs to the coin and
 * not to its size in the portfolio. Same palette, same folding rule, one implementation:
 * a second copy of this assignment is exactly how two charts come to disagree about which
 * colour a thing is.
 *
 * Anything the order does not carry falls to the Other grey rather than throwing, which is
 * the honest answer for a key with no place in the ranking.
 */
export function seriesColor(key: string, order: readonly string[]): string {
  const rank = order.indexOf(key)
  if (rank < 0 || rank >= SERIES_IDENTITIES) return OTHER_COLOR
  return rank < SERIES_HUES ? HUES[rank] : MUTED[rank - SERIES_HUES]
}

/**
 * A symbol's colour, by its rank in the response's `stack_order`.
 *
 * A projected payment for a security whose history the slice never mentioned is not in
 * the order, and the fold's own key is never in it; both land on Other through the same
 * branch.
 */
export function dividendColor(symbol: string, stackOrder: readonly string[]): string {
  return seriesColor(symbol, stackOrder)
}

/** True while the symbol is drawn as itself rather than folded away. */
export function hasIdentity(symbol: string, stackOrder: readonly string[]): boolean {
  const rank = stackOrder.indexOf(symbol)
  return rank >= 0 && rank < SERIES_IDENTITIES
}
