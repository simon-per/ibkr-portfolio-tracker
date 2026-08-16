import type { LookthroughResponse, LookthroughCompanyRow } from './api'

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
 */

/** How a company's exposure reaches the portfolio. Drives tile colour in both charts. */
export type HoldingKind = 'direct' | 'both' | 'via_funds'

/**
 * A treemap tile. The two structural roles are not companies — they carry value that belongs
 * on the chart so the areas mean what the header says, and they open no drill-down.
 */
export type TileRole = HoldingKind | 'other_companies' | 'unattributed'

export interface ExposureTile {
  /** React key. Prefixed for the structural tiles so it cannot collide with a company_key. */
  key: string
  name: string
  value: number
  /** Share of the WHOLE portfolio, matching the table's own column. */
  pct: number
  role: TileRole
  /** Set only for real companies, so a click can open the existing drill-down. */
  companyKey: string | null
  /** Recharts sizes a Treemap by a numeric dataKey; this is that key. */
  size: number
}

/**
 * Whether a company is held directly, through funds, or both.
 *
 * The `both` case is the one worth having: it is what tells "I own Nvidia and my funds also
 * own Nvidia" apart from "my funds own Apple and I never bought any", which is the single
 * observation this whole feature exists to make possible.
 */
export function holdingKind(row: LookthroughCompanyRow): HoldingKind {
  if (row.direct_value_eur > 0 && row.via_funds_value_eur > 0) return 'both'
  return row.direct_value_eur > 0 ? 'direct' : 'via_funds'
}

/** Everything the look-through could not attribute to a company. */
export function unattributedValue(data: LookthroughResponse): number {
  return data.uncovered_fund_eur + data.fund_residual_eur + data.nested_fund_eur
}

/**
 * The treemap's tiles: the companies on screen, the truncated tail, and the unattributed
 * remainder — which together cover the whole portfolio.
 *
 * Returns empty when there is no total to take a share of. That is the `concentrationPct`
 * refusal rather than a special case: a chart built from an unpriced portfolio would draw
 * confident rectangles out of nothing.
 */
export function buildExposureTiles(data: LookthroughResponse): ExposureTile[] {
  const total = data.total_market_value_eur
  if (!(total > 0)) return []

  const pctOf = (value: number) => (value / total) * 100
  const tiles: ExposureTile[] = data.companies.map((row) => ({
    key: row.company_key,
    name: row.name,
    value: row.value_eur,
    pct: row.pct_of_portfolio,
    role: holdingKind(row),
    companyKey: row.company_key,
    size: row.value_eur,
  }))

  if (data.other_companies_eur > 0) {
    tiles.push({
      key: 'tile:other',
      name:
        data.other_companies_count > 0
          ? `${data.other_companies_count} smaller companies`
          : 'Smaller companies',
      value: data.other_companies_eur,
      pct: pctOf(data.other_companies_eur),
      role: 'other_companies',
      companyKey: null,
      size: data.other_companies_eur,
    })
  }

  const unattributed = unattributedValue(data)
  if (unattributed > 0) {
    tiles.push({
      key: 'tile:unattributed',
      name: 'Not attributed to a company',
      value: unattributed,
      pct: pctOf(unattributed),
      role: 'unattributed',
      companyKey: null,
      size: unattributed,
    })
  }

  // A treemap cannot draw a zero and Recharts would still seat it in the hover layer, where
  // a phantom 0.00% row is indistinguishable from a real holding worth nothing.
  return tiles.filter((tile) => tile.size > 0)
}

/** One segment of the composition bar. */
export interface PartitionSegment {
  key: 'direct' | 'via_funds' | 'unattributed'
  label: string
  /** Why this slice is what it is, for the legend. */
  hint: string
  value: number
  pct: number
}

/**
 * The composition bar: every euro in the portfolio, split by how the look-through reaches it.
 *
 * **Three segments, not the response's five.** `fund_residual`, `nested_fund` and
 * `uncovered_fund` are one thing to a reader — value no company row accounts for — and
 * separating them here would spend three categorical hues on a distinction the fund table
 * below already makes in full, against the skill's own ~7-class ceiling and this file's
 * "a chart is not a table" rule. The partition still closes; it is just summed one level up.
 */
export function buildPartition(data: LookthroughResponse): PartitionSegment[] {
  const total = data.total_market_value_eur
  if (!(total > 0)) return []

  const segments: PartitionSegment[] = [
    {
      key: 'direct',
      label: 'Held directly',
      hint: 'Positions in the company itself, across every listing and share class.',
      value: data.direct_equity_eur,
      pct: (data.direct_equity_eur / total) * 100,
    },
    {
      key: 'via_funds',
      label: 'Through funds',
      hint: 'Company exposure found inside the funds whose baskets could be decomposed.',
      value: data.looked_through_equity_eur,
      pct: (data.looked_through_equity_eur / total) * 100,
    },
    {
      key: 'unattributed',
      label: 'Not attributed',
      hint:
        'Funds with no usable basket, plus the cash, derivatives and issuer rounding left ' +
        'over inside the funds that were decomposed.',
      value: unattributedValue(data),
      pct: (unattributedValue(data) / total) * 100,
    },
  ]

  // A zero segment draws nothing but still claims a legend row, which reads as a category
  // that exists and happens to be empty rather than one that does not apply.
  return segments.filter((segment) => segment.value > 0)
}
