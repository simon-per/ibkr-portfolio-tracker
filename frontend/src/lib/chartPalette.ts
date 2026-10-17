/**
 * The one categorical palette for every chart, plus the gain / loss / neutral colours.
 *
 * The values live in `index.css` (`--chart-1..10`, `--chart-other`, `--gain`, `--loss`, …),
 * per theme, with the measurements behind them; this module hands out references to them.
 * So no theme is read in JavaScript — a `var()` fill repaints itself when the theme flips —
 * and there is exactly one place that decides what slot 3 looks like. `chartPalette.test.ts`
 * reads the CSS back and re-measures it.
 *
 * Assignment stays with the caller and follows the entity, never its rank in the current
 * view (docs/frontend.md, *Colour by identity*): pass the series' position in a stable order
 * — a fixed taxonomy, the server's `stack_order`, the full benchmark list. Slots never cycle:
 * an index past the last slot folds to {@link CHART_OTHER}, because an eleventh series in a
 * recycled colour would claim to be the first.
 */

/** Slots with a hue of their own. */
export const CHART_SLOTS = 10

/** Slot `i` (0-based) as a CSS colour: `var(--chart-<i+1>)`. */
export const CHART_PALETTE: readonly string[] = Array.from(
  { length: CHART_SLOTS },
  (_, i) => `var(--chart-${i + 1})`,
)

/** The fold for everything past the last slot, and for an entity with no place in the order. */
export const CHART_OTHER = 'var(--chart-other)'

/** The colour of identity number `index` (0-based); out of range or non-integer → Other. */
export function chartColor(index: number): string {
  if (!Number.isInteger(index) || index < 0 || index >= CHART_SLOTS) return CHART_OTHER
  return CHART_PALETTE[index]
}

/**
 * A key's colour by its position in a stable identity order. Anything the order does not
 * carry falls to Other rather than throwing — the honest answer for a key with no rank.
 */
export function chartColorFor(key: string, order: readonly string[]): string {
  return chartColor(order.indexOf(key))
}

/** Semantic marks, for a bar or a line whose colour says up / down rather than which. */
export const GAIN_COLOR = 'hsl(var(--gain))'
export const LOSS_COLOR = 'hsl(var(--loss))'
export const WARN_COLOR = 'hsl(var(--warn))'
export const INFO_COLOR = 'hsl(var(--info))'
/** Flat, absent, or not a direction at all. */
export const NEUTRAL_COLOR = 'hsl(var(--muted-foreground))'

/**
 * Gain above `+flatBand`, loss below `-flatBand`, neutral inside it or when unknown.
 *
 * `flatBand` defaults to 0 — every move is a direction — which suits returns and P&L. A
 * lumpy series (dividends, contributions) passes `FLAT_BAND_PCT` from `lib/delta.ts`, the
 * same band `DeltaChip` uses, so a bar and its chip never disagree about "up".
 */
export function signColor(value: number | null | undefined, flatBand = 0): string {
  if (value == null || !Number.isFinite(value)) return NEUTRAL_COLOR
  if (value > flatBand) return GAIN_COLOR
  if (value < -flatBand) return LOSS_COLOR
  return NEUTRAL_COLOR
}
