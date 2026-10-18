import { displayName } from './securityName'
import type { LookthroughResponse } from './api'
import { CASH_BUCKET, UNKNOWN_SECTOR, sectorGroup } from './sectorColors'

/**
 * What the two look-through charts draw, as pure data.
 *
 * Separate from the components for the reason `portfolioKpis.ts` and `rebalance.ts` are:
 * the interesting mistakes here are arithmetic, and arithmetic is testable in `node`
 * without paying jsdom's startup for it.
 *
 * **One rule governs both builders, and it is the same one the table follows: nothing is
 * renormalised onto the covered part.** A treemap whose tiles are only the companies would
 * fill the whole card with 78% of the book and draw every tile as though it were a share of
 * the whole — silently inflating each one by the undecomposed remainder. So the remainder is
 * a tile. It is a large grey block, and that is the honest picture rather than a defect in
 * it: `coverage_pct` says the same thing in a number nobody looks at twice.
 *
 * **The whole is `total_value_eur`: holdings plus positive uninvested cash**, the base the
 * table's `pct_of_portfolio`, Allocation and Positions all divide by (GitHub #28). So cash is a
 * block in both charts too, or the areas would quietly be shares of the holdings again.
 *
 * The treemap groups companies by **sector**; it used to colour them by whether they were held
 * directly, through funds, or both. That was dropped at the owner's request — the table's
 * Direct / Via funds columns and the drill-down still carry it, and only the treemap's one
 * colour channel had to choose.
 */

/**
 * A leaf: one company, or one of the structural blocks. The structural ones are not
 * companies — they carry value that belongs on the chart so the areas mean what the header
 * says — and they open no drill-down.
 */
export interface ExposureTile {
  /** React key. Prefixed for the structural tiles so it cannot collide with a company_key. */
  key: string
  name: string
  value: number
  /** Share of the WHOLE portfolio, matching the table's own column. */
  pct: number
  /** The company's real sector, for the tooltip — not the group it was folded into. */
  sector: string
  /** The company's ISINs and listing keys, for its avatar; empty for a structural tile. */
  isins: string[]
  listings: string[]
  /** Set only for real companies, so a click can open the existing drill-down. */
  companyKey: string | null
  /** Recharts sizes a Treemap by a numeric dataKey; this is that key. */
  size: number
}

/** A depth-1 node: a sector, or one of the structural blocks. */
export interface ExposureGroup {
  key: string
  name: string
  value: number
  pct: number
  /** Which paint slot to use — a canonical sector, `Other sectors`, or a structural role. */
  paintKey: string
  size: number
  children: ExposureTile[]
}

/** Everything the look-through could not attribute to a company. */
export function unattributedValue(data: LookthroughResponse): number {
  return data.uncovered_fund_eur + data.fund_residual_eur + data.nested_fund_eur
}

/**
 * The Cash row, or null when there is none. Its presence is the server's
 * `cash_pct_of_portfolio`, never a sign test here: unknown and negative balances are decided
 * once, in `CashService.uninvested_cash`.
 */
export function cashRow(data: LookthroughResponse): { value: number; pct: number } | null {
  if (data.cash_pct_of_portfolio == null || data.cash_eur == null) return null
  return { value: data.cash_eur, pct: data.cash_pct_of_portfolio }
}

/**
 * The treemap, as sector groups of company tiles plus the structural blocks: the truncated tail,
 * the unattributed remainder and uninvested cash.
 *
 * **The structural blocks are top-level groups, not tiles inside a sector**, because none is a
 * sector: the truncated tail spans all of them, and the unattributed remainder and cash belong to
 * no company at all. Keeping them at depth 1 is also what preserves the partition — every leaf still
 * sums to the whole portfolio, which is the one property worth testing.
 *
 * Returns empty when there is no total to take a share of. That is the `concentrationPct` refusal
 * rather than a special case: a chart built from an unpriced portfolio would draw confident
 * rectangles out of nothing.
 */
export function buildExposureGroups(data: LookthroughResponse): ExposureGroup[] {
  const total = data.total_value_eur
  if (!(total > 0)) return []

  const pctOf = (value: number) => (value / total) * 100
  const bySector = new Map<string, ExposureTile[]>()

  for (const row of data.companies) {
    if (!(row.value_eur > 0)) continue
    const group = sectorGroup(row.sector)
    const tile: ExposureTile = {
      key: row.company_key,
      name: displayName(row.name),
      value: row.value_eur,
      pct: row.pct_of_portfolio,
      // The row's own sector, not `group` — a company folded into `Other sectors` must still be
      // able to say what it actually is, or the fold looks like missing data.
      sector: row.sector || UNKNOWN_SECTOR,
      isins: row.isins,
      listings: row.listings,
      companyKey: row.company_key,
      size: row.value_eur,
    }
    const bucket = bySector.get(group)
    if (bucket) bucket.push(tile)
    else bySector.set(group, [tile])
  }

  const groups: ExposureGroup[] = []
  for (const [name, children] of bySector) {
    children.sort((a, b) => b.value - a.value || a.key.localeCompare(b.key))
    const value = children.reduce((acc, tile) => acc + tile.value, 0)
    groups.push({
      key: `sector:${name}`,
      name,
      value,
      pct: pctOf(value),
      paintKey: name,
      size: value,
      children,
    })
  }

  const structural = (
    key: 'other_companies' | 'unattributed' | 'cash',
    name: string,
    value: number,
    pct: number = pctOf(value),
  ): void => {
    if (!(value > 0)) return
    const tile: ExposureTile = {
      key: `tile:${key}`,
      name,
      value,
      pct,
      sector: '',
      isins: [],
      listings: [],
      companyKey: null,
      size: value,
    }
    groups.push({
      key: `group:${key}`,
      name,
      value,
      pct,
      paintKey: key,
      size: value,
      // A single child so every leaf in the tree is an `ExposureTile` and the sum test can
      // walk one shape. Recharts draws the child, which is what carries the label.
      children: [tile],
    })
  }

  structural(
    'other_companies',
    data.other_companies_count > 0
      ? `${data.other_companies_count} smaller companies`
      : 'Smaller companies',
    data.other_companies_eur,
  )
  structural('unattributed', 'Not attributed to a company', unattributedValue(data))
  // The server's share, so the tile and the table's Cash row are one figure.
  const cash = cashRow(data)
  if (cash) structural('cash', CASH_BUCKET, cash.value, cash.pct)

  // Largest first, ties on name, so the layout cannot shuffle between requests.
  groups.sort((a, b) => b.value - a.value || a.name.localeCompare(b.name))
  return groups
}

/** Every leaf across every group — what the partition assertion walks. */
export function allTiles(groups: ExposureGroup[]): ExposureTile[] {
  return groups.flatMap((group) => group.children)
}

/** One segment of the composition bar. */
export interface PartitionSegment {
  key: 'direct' | 'via_funds' | 'unattributed' | 'cash'
  label: string
  value: number
  pct: number
}

/**
 * The composition bar: every euro in the portfolio, split by how the look-through reaches it.
 *
 * **Three holdings segments, not the response's five.** `fund_residual`, `nested_fund` and
 * `uncovered_fund` are one thing to a reader — value no company row accounts for — and
 * separating them here would spend three categorical hues on a distinction the fund table
 * below already makes in full, against the skill's own ~7-class ceiling and this file's
 * "a chart is not a table" rule. The partition still closes; it is just summed one level up.
 * Uninvested cash is a fourth segment when there is a Cash row, so the bar's percentages are
 * shares of the same whole as the table's.
 */
export function buildPartition(data: LookthroughResponse): PartitionSegment[] {
  const total = data.total_value_eur
  if (!(total > 0)) return []
  const cash = cashRow(data)

  const segments: PartitionSegment[] = [
    {
      key: 'direct',
      label: 'Held directly',
      value: data.direct_equity_eur,
      pct: (data.direct_equity_eur / total) * 100,
    },
    {
      key: 'via_funds',
      label: 'Through funds',
      value: data.looked_through_equity_eur,
      pct: (data.looked_through_equity_eur / total) * 100,
    },
    {
      key: 'unattributed',
      label: 'Not attributed',
      value: unattributedValue(data),
      pct: (unattributedValue(data) / total) * 100,
    },
    {
      key: 'cash',
      label: CASH_BUCKET,
      value: cash?.value ?? 0,
      pct: cash?.pct ?? 0,
    },
  ]

  // A zero segment draws nothing but still claims a legend row, which reads as a category
  // that exists and happens to be empty rather than one that does not apply.
  return segments.filter((segment) => segment.value > 0)
}
