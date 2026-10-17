import type { FooterCell } from './DataTable'

/** One `<td>` of the footer row: a footer cell, or padding under the columns nobody named. */
export interface FooterSlot {
  cell: FooterCell | null
  colSpan: number
}

/**
 * The footer row's cells in column order, each under the columns its `spans` name.
 *
 * The footer used to be laid out in **array** order with `colSpan = spans.length`, so the
 * spans decided a cell's width and nothing decided its place. Three of the four tax tables
 * listed their totals in a different order from their columns from the day they became
 * `DataTable`s (2026-10-12) until 2026-10-17, and every total sat under a neighbour's
 * header — the dividend net captioned Withholding, the holdings total under Quantity —
 * while every test stayed green, because each figure was still somewhere on the page.
 * Deriving the position from the column keys makes the order of the array irrelevant.
 *
 * Throws on spans that are not adjacent or that two cells share: a total that cannot be
 * placed under the column it sums is refused rather than drawn somewhere plausible.
 * Spans naming no visible column (a `desktop: 'hide'` one) are dropped with their cell.
 */
export function footerLayout(
  footer: readonly FooterCell[],
  columnKeys: readonly string[],
  columnCount: number = columnKeys.length,
): FooterSlot[] {
  const index = new Map(columnKeys.map((k, i) => [k, i]))
  const placed: { cell: FooterCell; start: number; end: number }[] = []
  for (const cell of footer) {
    const at = cell.spans.map(k => index.get(k)).filter((i): i is number => i !== undefined)
    if (at.length === 0) continue
    const start = Math.min(...at)
    const end = Math.max(...at)
    if (end - start + 1 !== at.length) {
      throw new Error(
        `Footer cell "${cell.key}" spans columns that are not adjacent (${cell.spans.join(', ')})`,
      )
    }
    placed.push({ cell, start, end })
  }
  placed.sort((a, b) => a.start - b.start)

  const slots: FooterSlot[] = []
  let next = 0
  for (const { cell, start, end } of placed) {
    if (start < next) {
      throw new Error(`Footer cell "${cell.key}" overlaps another footer cell`)
    }
    if (start > next) slots.push({ cell: null, colSpan: start - next })
    slots.push({ cell, colSpan: end - start + 1 })
    next = end + 1
  }
  if (next < columnCount) slots.push({ cell: null, colSpan: columnCount - next })
  return slots
}
