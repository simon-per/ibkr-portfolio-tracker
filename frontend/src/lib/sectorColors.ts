/**
 * Which canonical sectors get their own colour, and which fold together.
 *
 * The hexes live in `index.css` as `--viz-sector-*`; this module owns only the *assignment*, so
 * there is one place that decides "Technology is slot 1" and one place that decides what slot 1
 * looks like in each theme.
 *
 * **Four hues, because four is what passes.** A treemap can seat any group beside any other, so
 * the honest colourblind check is all-pairs. Brute-forcing every subset of the eight-hue
 * categorical set against both surfaces found no passing set of five and exactly two of four.
 * See the comment beside `--viz-sector-1` for the measurements.
 *
 * **The mapping is fixed, never derived from the current portfolio.** Colour follows the entity,
 * not its rank: if hues were handed to whichever sectors happen to be biggest, buying into
 * healthcare would silently turn Technology yellow and every reader who had learned the chart
 * would be misled. The cost is that a sector outside the four cannot get its own hue however
 * large it grows — which is visible rather than hidden, because `Other sectors` shows its own
 * share. If the book rotates, re-order `CANONICAL_SECTORS` on the server and this list with it.
 */

/** Mirrors `sector_taxonomy.CANONICAL_SECTORS[:4]` on the backend. */
export const HUED_SECTORS = [
  'Technology',
  'Communications',
  'Financials',
  'Consumer Cyclical',
] as const

/** What the server sends when nothing classified a company. */
export const UNKNOWN_SECTOR = 'Unknown'

/** The group every sector outside `HUED_SECTORS` — and `Unknown` — folds into. */
export const OTHER_SECTORS_LABEL = 'Other sectors'

export interface SectorPaint {
  fill: string
  ink: string
}

const SECTOR_PAINT: Record<string, SectorPaint> = {
  Technology: { fill: 'var(--viz-sector-1)', ink: 'var(--viz-sector-1-ink)' },
  Communications: { fill: 'var(--viz-sector-2)', ink: 'var(--viz-sector-2-ink)' },
  Financials: { fill: 'var(--viz-sector-3)', ink: 'var(--viz-sector-3-ink)' },
  'Consumer Cyclical': { fill: 'var(--viz-sector-4)', ink: 'var(--viz-sector-4-ink)' },
  [OTHER_SECTORS_LABEL]: {
    fill: 'var(--viz-sector-other)',
    ink: 'var(--viz-sector-other-ink)',
  },
}

/** The two structural groups: value on the chart to keep the areas honest, not companies. */
export const STRUCTURAL_PAINT: Record<'other_companies' | 'unattributed', SectorPaint> = {
  other_companies: { fill: 'var(--viz-other)', ink: 'var(--viz-other-ink)' },
  unattributed: { fill: 'var(--viz-unattributed)', ink: 'var(--viz-unattributed-ink)' },
}

/**
 * The group a company's sector belongs to.
 *
 * `Unknown` folds in with the small sectors rather than getting a group of its own. On this book
 * that is one company worth 0.04%, and a lone grey tile captioned "Unknown" reads as a fault
 * where "Other sectors" reads as what it is — the part of the chart not worth its own colour.
 * The tooltip still names the real sector, so nothing is hidden by the fold.
 */
export function sectorGroup(sector: string | null | undefined): string {
  const name = (sector ?? '').trim()
  return (HUED_SECTORS as readonly string[]).includes(name) ? name : OTHER_SECTORS_LABEL
}

/**
 * Paint for any group the treemap draws — a hued sector, the fold, or a structural block.
 *
 * **It has to consult both records.** Looking only at `SECTOR_PAINT` made every unmatched key
 * fall through to the `Other sectors` grey, so `Not attributed to a company` and the truncated
 * tail rendered in exactly the same colour as each other and as the fold — three different
 * meanings, one fill, and the legend showing three identical swatches. Nothing failed: the
 * fallback is legitimate for a genuinely unknown group, which is what made it invisible until
 * the pixels were sampled.
 */
export function sectorPaint(group: string): SectorPaint {
  return (
    SECTOR_PAINT[group] ??
    STRUCTURAL_PAINT[group as keyof typeof STRUCTURAL_PAINT] ??
    SECTOR_PAINT[OTHER_SECTORS_LABEL]
  )
}

/**
 * Every canonical sector, in order — mirrors `sector_taxonomy.CANONICAL_SECTORS`.
 *
 * The look-through only ever needs the first four; the Allocation tab draws all eleven as
 * sibling tiles, which is the same all-pairs problem with no fold available, since folding
 * would destroy the very split that chart exists to show.
 */
export const CANONICAL_SECTORS = [
  'Technology',
  'Communications',
  'Financials',
  'Consumer Cyclical',
  'Healthcare',
  'Industrials',
  'Consumer Defensive',
  'Energy',
  'Basic Materials',
  'Real Estate',
  'Utilities',
] as const

/**
 * De-emphasis steps for the sectors past the four hued ones, ordered light to dark.
 *
 * Not a value ramp — the order is *canonical position*, so a sector keeps its step whatever it
 * is worth today. Seven distinguishable neutrals beside four saturated hues is the "emphasis"
 * pattern: the four that carry most of a typical book read as identities, the rest read as a
 * group while still being told apart by lightness and by the tile's own label.
 *
 * This replaces eleven saturated hues that could not work. Measured on the old set,
 * `#8b5cf6` Communications against `#a855f7` Basic Materials was **ΔE 0.3 under protanopia and
 * 5.1 with full colour vision** — two adjacent tiles nobody could separate, on the chart whose
 * only job is separating them.
 */
const NEUTRAL_STEPS = [
  '#7b8592', '#6d7885', '#5f6a78', '#525d6b', '#45505e', '#3a4452', '#303947',
] as const

/**
 * A fixed grey for the bucket that means "we could not classify this".
 *
 * Fixed, because it used to take `FALLBACK_COLORS[index % 15]` — a colour decided by where the
 * bucket happened to sort, so it changed on every sync that reordered the chart.
 */
export const UNKNOWN_FILL = '#7d7a74'

/**
 * Every neutral above, and `UNKNOWN_FILL`, clears 3:1 against white — that is what lets the
 * Allocation treemap keep one label ink for the whole de-emphasis ramp. The four hued slots do
 * not: light `--viz-sector-2` measures 2.17 against white, which is why the paint carries its
 * own ink rather than the renderer assuming one.
 */
const NEUTRAL_INK = '#ffffff'

/**
 * The Allocation tab's colour for a sector, by identity and never by row position.
 *
 * The old `getColor(name, map, index)` fell back to `FALLBACK_COLORS[index % 15]`, so any
 * sector without an entry — `Unknown` among them — changed colour whenever the ordering
 * changed. Colour following rank is the one thing a categorical scale must never do.
 */
export function allocationSectorPaint(name: string): SectorPaint {
  const trimmed = name.trim()
  const hued = (HUED_SECTORS as readonly string[]).indexOf(trimmed)
  if (hued >= 0) {
    return {
      fill: `var(--viz-sector-${hued + 1})`,
      ink: `var(--viz-sector-${hued + 1}-ink)`,
    }
  }
  const position = (CANONICAL_SECTORS as readonly string[]).indexOf(trimmed)
  if (position >= 0) {
    return {
      fill: NEUTRAL_STEPS[(position - HUED_SECTORS.length) % NEUTRAL_STEPS.length],
      ink: NEUTRAL_INK,
    }
  }
  return neutralPaint(trimmed)
}

/**
 * A stable de-emphasis colour for a label no palette covers.
 *
 * Derived from the name, never from the row's position, so the same category keeps the same
 * colour when a filter or a re-sort changes what is on screen. The region and asset-type charts
 * use it for the same reason the sector one does.
 */
export function neutralPaint(name: string): SectorPaint {
  const trimmed = name.trim()
  if (trimmed === 'Unknown' || trimmed === '') return { fill: UNKNOWN_FILL, ink: NEUTRAL_INK }
  let hash = 0
  for (const ch of trimmed) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0
  return { fill: NEUTRAL_STEPS[hash % NEUTRAL_STEPS.length], ink: NEUTRAL_INK }
}
