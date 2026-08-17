import type { PortfolioValuePoint } from './api'
import { externalFlow, isMeasurable } from './portfolioKpis'

export interface MonthReturn {
  returnPercent: number
  startValue: number
  endValue: number
  newInvestment: number
  /**
   * The window was shortened because its edge days could not be fully valued.
   * The figure is a true Modified Dietz return over the days it kept — just not
   * over the whole period its label names.
   */
  partial?: boolean
  /**
   * The days the figure actually covers, set in the same breath as `partial` so the
   * two cannot separate. Without it the label is the only thing naming a period, and
   * the label is the thing that is wrong: a trimmed "YTD" read +3.1% over six weeks
   * of a year, badged with a dagger that said "part of the period" and not which part.
   */
  measured?: { from: string; to: string }
}

/**
 * Modified Dietz return over one period of daily points, flows day-weighted.
 *
 * Flows come from `externalFlow`, which values a sale at its market proceeds.
 * Inferring them from the cost-basis line instead is wrong on every sale date:
 * cost basis falls by what the lot cost while market value falls by what it
 * sold for, so the difference prints as a fabricated loss — a same-day
 * rotation showed its realized gain as a negative monthly return.
 *
 * **Edge days the backend could not fully value are trimmed off.** This is the
 * third consumer of `unpriced_holdings`, after `dailyReturnSeries` and
 * `betaAndCorrelation`, and the one missed when those two were guarded. Only the
 * endpoints enter the arithmetic, so an incomplete one is not a small error:
 * `market_value_eur` omits the holdings that could not be priced while the flows
 * do not, so a stale end reads as a loss and a stale start as a gain. Past the
 * backend's 14-day lookback every holding drops out and the period prints -100%.
 * The YTD column is the one that matters most — it ends on *today*, which is
 * exactly the day a stalled feed breaks.
 *
 * Trimming rather than refusing, because Modified Dietz reads only the endpoint
 * values and the flows between them: the result over the kept days is exact
 * rather than an approximation, and discarding a whole month over one stale day
 * would lose far more than it protects. `partial` reports that the window was
 * shortened. Interior gaps need no handling — nothing reads an interior market
 * value.
 */
export function computeModifiedDietzReturn(points: PortfolioValuePoint[]): MonthReturn | null {
  let lo = 0
  let hi = points.length - 1
  while (lo <= hi && !isMeasurable(points[lo])) lo++
  while (hi > lo && !isMeasurable(points[hi])) hi--

  const partial = lo !== 0 || hi !== points.length - 1
  const window = partial ? points.slice(lo, hi + 1) : points

  if (window.length < 2) return null
  const startMV = window[0].market_value_eur
  const endMV = window[window.length - 1].market_value_eur
  if (startMV === 0) return null

  const totalDays = window.length - 1

  let netCashFlow = 0
  let weightedCashFlow = 0
  for (let i = 1; i < window.length; i++) {
    const cf = externalFlow(window[i - 1], window[i])
    if (cf !== 0) {
      netCashFlow += cf
      // Weight: the fraction of the period the flow was actually at work
      weightedCashFlow += cf * ((totalDays - i) / totalDays)
    }
  }

  const gain = endMV - startMV - netCashFlow
  const denominator = startMV + weightedCashFlow
  // Nothing to divide by means the return is undefined, not flat. Reachable when a
  // withdrawal early in the period exceeds the starting value — the weighted flow then
  // outweighs it — and a `0.00%` cell claiming a quiet month is exactly the plausible
  // stand-in this codebase keeps having to remove. The two undefined cases above already
  // return null and every caller renders a dash for it.
  if (!(denominator > 0)) return null

  return {
    returnPercent: (gain / denominator) * 100,
    startValue: startMV,
    endValue: endMV,
    newInvestment: netCashFlow,
    ...(partial
      ? { partial: true, measured: { from: window[0].date, to: window[window.length - 1].date } }
      : {}),
  }
}
