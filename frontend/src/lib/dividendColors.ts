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
 * The colours are the app's one categorical palette (`lib/chartPalette.ts`, 2026-10-17): ten
 * hues, then the Other grey. This module owns only the assignment and the fold; what slot 3
 * looks like in each theme, and the measurements behind it, belong to the palette. It used to
 * carry its own eight hues plus a five-step muted ramp (`--viz-series-*`) — thirteen
 * identities, but the ramp read as near-black bars in light mode and was a second palette
 * beside the app's. Ten distinct hues for a slightly larger fold is the trade taken.
 *
 * The order does move as the book grows — a holding overtaking another swaps two slots
 * between sessions. That is the same caveat `benchmarkColors.ts` carries, and the only
 * alternative that never moves (ordering by first payment date) hands the hues to the oldest
 * payers rather than the biggest, which is not what a reader is looking for.
 */
import { CHART_OTHER, CHART_SLOTS, chartColorFor } from './chartPalette'

/** Ranks 1..10 are drawn as themselves. Everything after folds into Other. */
export const SERIES_IDENTITIES = CHART_SLOTS

export const OTHER_COLOR = CHART_OTHER

/**
 * A key's colour, by its position in an identity order the server computed.
 *
 * It IS `chartColorFor`, not a copy of it — the crypto donut calls that directly with its own
 * `color_order` — because a second copy of this assignment is exactly how two charts come to
 * disagree about which colour a thing is. Anything the order does not carry falls to Other
 * rather than throwing.
 */
export const seriesColor: (key: string, order: readonly string[]) => string = chartColorFor

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
