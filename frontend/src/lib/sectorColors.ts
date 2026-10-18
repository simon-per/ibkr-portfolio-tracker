/**
 * Which colour a sector, a region or an asset type gets — on the Allocation donuts and in the
 * look-through treemap — and which sectors the treemap folds together.
 *
 * Every fill is a slot of the one categorical palette (`lib/chartPalette.ts`, measured in
 * `index.css`) or one of `lib/chartPaint.ts`'s three neutrals. This module owns only the
 * *assignment*: one place decides "Technology is slot 1", one place decides what slot 1 looks
 * like in each theme.
 *
 * **The assignment is a fixed identity order, never derived from the current portfolio.**
 * Colour follows the entity, not its rank (docs/frontend.md, *Colour by identity*): if hues
 * went to whichever sectors happen to be largest, buying into healthcare would repaint
 * Technology and every reader who had learned the chart would be misled. The palette never
 * cycles, so an entity past slot ten takes Other — visible, because the legend names it.
 *
 * **Three reserved buckets keep three meanings apart**: `Cash` is the strong neutral, a bucket
 * nothing classified (`Unknown`, the region chart's `Other`) the faint one, and the palette's
 * own tail the plain Other. Three meanings in one grey drew three identical legend swatches
 * once already (docs/lookthrough.md).
 */
import { CHART_OTHER, chartColor } from './chartPalette'
import { HATCHED_NEUTRAL, NEUTRAL_PAINT, chartPaint, type ChartPaint } from './chartPaint'

export type SectorPaint = ChartPaint

/**
 * Every canonical sector, in order — mirrors `sector_taxonomy.CANONICAL_SECTORS`. Position is
 * the palette slot: ten hues, and the eleventh (Utilities) takes Other.
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

/** The regions `AllocationTab.regionOf` can produce, in slot order. */
export const REGION_ORDER = [
  'North America',
  'Europe',
  'Asia Pacific',
  'Emerging Markets',
  'Latin America',
  'Middle East & Africa',
] as const

/** The asset classes the allocation service writes, in slot order. */
export const ASSET_TYPE_ORDER = ['Stock', 'ETF'] as const

/** What the server sends when nothing classified a company or a holding. */
export const UNKNOWN_SECTOR = 'Unknown'

/** The bucket the allocation service adds to every breakdown for uninvested cash, and the
 * look-through's Cash row. */
export const CASH_BUCKET = 'Cash'

/** Labels that mean "not classified" rather than naming a category. */
const UNCLASSIFIED = new Set(['', UNKNOWN_SECTOR, 'Other'])

/**
 * The paint for one allocation bucket, by its place in `order`.
 *
 * Cash and the unclassified buckets are checked first, so no order can hand them a hue.
 */
export function bucketPaint(name: string, order: readonly string[]): SectorPaint {
  const trimmed = name.trim()
  if (trimmed === CASH_BUCKET) return NEUTRAL_PAINT.strong
  if (UNCLASSIFIED.has(trimmed)) return NEUTRAL_PAINT.faint
  return chartPaint(order.indexOf(trimmed))
}

/** The Allocation tab's colour for a sector. */
export function allocationSectorPaint(name: string): SectorPaint {
  return bucketPaint(name, CANONICAL_SECTORS)
}

export function regionPaint(name: string): SectorPaint {
  return bucketPaint(name, REGION_ORDER)
}

export function assetTypePaint(name: string): SectorPaint {
  return bucketPaint(name, ASSET_TYPE_ORDER)
}

/**
 * A label no palette covers (the Analytics tab's countries): the reserved buckets keep their
 * neutrals and everything else is the plain Other — never a colour derived from row position.
 */
export function neutralPaint(name: string): SectorPaint {
  return bucketPaint(name, [])
}

/** The treemap group every sector without a hue of its own — and `Unknown` — folds into. */
export const OTHER_SECTORS_LABEL = 'Other sectors'

/**
 * The treemap group a company's sector belongs to: its own if the sector has a hue, else the
 * fold. One rule for both tabs — a sector is grouped exactly when it is coloured, so a sector
 * is one colour wherever it is drawn.
 *
 * `Unknown` folds in with the tail rather than getting a group of its own: a lone grey tile
 * captioned "Unknown" reads as a fault, and the tooltip still names the real sector.
 */
export function sectorGroup(sector: string | null | undefined): string {
  const name = (sector ?? '').trim()
  const slot = (CANONICAL_SECTORS as readonly string[]).indexOf(name)
  return slot >= 0 && chartColor(slot) !== CHART_OTHER ? name : OTHER_SECTORS_LABEL
}

/**
 * The structural treemap groups: value on the chart to keep the areas honest, not companies.
 *
 * Cash is the Allocation tab's Cash paint (`bucketPaint`), because it is the same money on
 * both tabs and colour follows the entity. That spends the strong neutral, so "Not attributed"
 * — the caveat — is the strong neutral striped with the faint one: still the heaviest grey,
 * and distinct from Cash beside it. The truncated tail is the faint one; the sector fold sits
 * between them as the palette's Other.
 */
export const STRUCTURAL_PAINT: Record<'other_companies' | 'unattributed' | 'cash', SectorPaint> = {
  other_companies: NEUTRAL_PAINT.faint,
  unattributed: HATCHED_NEUTRAL,
  cash: bucketPaint(CASH_BUCKET, []),
}

/**
 * Paint for any group the treemap draws — a hued sector, the fold, or a structural block.
 *
 * It consults the structural record as well as the sectors: looking only at sectors once made
 * `Not attributed` and the truncated tail fall through to the fold's grey — three meanings,
 * one fill, and nothing failed, because the fallback is right for a genuinely unknown group.
 */
export function sectorPaint(group: string): SectorPaint {
  if (Object.hasOwn(STRUCTURAL_PAINT, group)) {
    return STRUCTURAL_PAINT[group as keyof typeof STRUCTURAL_PAINT]
  }
  const slot = (CANONICAL_SECTORS as readonly string[]).indexOf(group)
  return slot >= 0 ? chartPaint(slot) : NEUTRAL_PAINT.tail
}
