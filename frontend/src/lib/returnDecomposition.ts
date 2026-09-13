/**
 * The waterfall behind the Analytics tab's return decomposition chart.
 *
 * Pure: takes one `DecompositionWindow` and lays its legs out as bars that walk from the
 * start value to the end value. Kept out of the component so it can be tested without a
 * DOM and so the component file exports only components (the react-refresh rule).
 */
import type { DecompositionWindow } from './api'

export type LegKey =
  | 'price_effect_eur'
  | 'fx_effect_eur'
  | 'dividends_eur'
  | 'cash_adjustment_eur'
  | 'unsplit_eur'
  | 'unexplained_eur'

/** The legs in drawing order, with the label each bar and legend entry carries. */
export const LEGS: { key: LegKey; label: string }[] = [
  { key: 'price_effect_eur', label: 'Price' },
  { key: 'fx_effect_eur', label: 'FX' },
  { key: 'dividends_eur', label: 'Dividends' },
  { key: 'cash_adjustment_eur', label: 'Cash adjustment' },
  { key: 'unsplit_eur', label: 'Unsplit' },
  { key: 'unexplained_eur', label: 'Unexplained' },
]

export interface WaterfallRow {
  name: string
  /** Invisible offset the visible bar sits on. */
  base: number
  /** Visible bar height (always ≥ 0). */
  size: number
  /** The signed figure the bar represents. */
  value: number
  kind: 'total' | 'flow' | 'gain' | 'loss'
}

/**
 * Start → paid in → each leg → end. A `null` or zero leg is skipped rather than drawn as a
 * zero-height bar that would still show a figure in the tooltip. Empty when either endpoint
 * is unknown: there is no start to walk from.
 */
export function waterfallRows(w: DecompositionWindow): WaterfallRow[] {
  if (w.start_total_value_eur === null || w.end_total_value_eur === null) return []
  const rows: WaterfallRow[] = []
  let running = w.start_total_value_eur
  rows.push({ name: 'Start', base: 0, size: running, value: running, kind: 'total' })
  const step = (name: string, v: number | null, kind: WaterfallRow['kind'] | null) => {
    if (v === null || v === 0) return
    const from = running
    running += v
    rows.push({
      name,
      base: Math.min(from, running),
      size: Math.abs(v),
      value: v,
      kind: kind ?? (v >= 0 ? 'gain' : 'loss'),
    })
  }
  step('Paid in', w.net_flows_eur, 'flow')
  for (const leg of LEGS) step(leg.label, w[leg.key], null)
  rows.push({ name: 'End', base: 0, size: w.end_total_value_eur, value: w.end_total_value_eur, kind: 'total' })
  return rows
}
