import type { PortfolioValuePoint } from './api'

/**
 * The value chart's Profit/Loss line, rebased to 0 on the first point of the range.
 *
 * The absolute gap between the value line and its baseline is profit since inception, so on
 * 3M the line used to start at two years of history the range does not draw — the same
 * misreading the benchmark had until it moved to `anchor=window` on 2026-09-07. The anchor is
 * `data[0]`, the point the benchmark seed is read from, so every line on the chart starts on
 * the same day.
 *
 * **A shift is right here and was wrong for the benchmark.** The gap is already net of
 * contributions — Money In (or Cost Basis) rises by the same amount as the value line on a
 * deposit — so subtracting a constant leaves in-window flows out of it. Scaling the benchmark
 * would have scaled the deposits too; nothing is scaled here.
 */

/** The gap between whichever pair the chart draws; see `cashIsTracked` for the mode. */
export function rawProfit(point: PortfolioValuePoint, cashTracked: boolean): number {
  return cashTracked
    ? (point.total_value_eur ?? 0) - (point.money_in_eur ?? 0)
    : point.market_value_eur - point.cost_basis_eur
}

/** One value per point: profit made since the first point, which is exactly 0. */
export function rangeProfitSeries(data: PortfolioValuePoint[], cashTracked: boolean): number[] {
  if (data.length === 0) return []
  const anchor = rawProfit(data[0], cashTracked)
  return data.map((p) => rawProfit(p, cashTracked) - anchor)
}

/**
 * Holdings the anchor day could not price. Above 0 the anchor's value is short by their whole
 * value, so every later point is **overstated** by that shortfall — the opposite direction to
 * an unpriced day inside the range, which understates only itself.
 */
export function rangeProfitAnchorUnpriced(data: PortfolioValuePoint[]): number {
  return data[0]?.unpriced_holdings ?? 0
}
