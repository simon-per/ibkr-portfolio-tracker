import type { DividendTtmPoint } from './api'

export interface DividendPace {
  /** Compound monthly growth rate, percent. */
  cmgr_pct: number
  /** The same rate over a year. null when the span is under twelve months. */
  cagr_pct: number | null
  from_month: string
  to_month: string
  from_eur: number
  to_eur: number
  /** Months between the two anchors. */
  months: number
  /** An anchor window carries projection, so the rate is forward-looking. */
  includes_forecast: boolean
  /** The base is the earliest window on record — see the function's note. */
  coverage_limited: boolean
}

/** Whole months from one "YYYY-MM" to another. */
function monthsBetween(from: string, to: string): number {
  const [fy, fm] = from.split('-').map(Number)
  const [ty, tm] = to.split('-').map(Number)
  return (ty * 12 + tm) - (fy * 12 + fm)
}

/**
 * How fast the rolling twelve-month total grew across the windows on screen.
 *
 * Measured between the FIRST and LAST displayed window, which is what makes it
 * answer the range the reader selected — the previous version used a fixed
 * six-month lookback anchored at the projection horizon and so read the same
 * whichever range was showing, which is not a growth rate for anything.
 *
 * `points` is `ttmPoints` from {@link buildChartSeries}: already sliced to the
 * range by the server and already stripped of open windows when the Forecast
 * toggle is off. That array is the single statement of which windows are
 * displayed, so the rate is derived from it rather than from a second copy of
 * the same rule — which is also why this is computed here and not server-side.
 *
 * Endpoints rather than a fitted slope: the two anchors are the first and last
 * bar on the chart, so the figure can be checked against what is visible. A
 * regression over every window is less sensitive to one odd endpoint but has no
 * answer to "which two bars is this comparing".
 *
 * `coverage_limited` marks the case where the base is the earliest window that
 * has ever existed. On a portfolio still being built, that window holds a
 * fraction of a year's income and the growth out of it is enormous and nearly
 * meaningless — the `yoy_vs_partial` shape, and it carries the same marker.
 * `coverageStart` has to come from the server: a windowed response cannot say
 * whether an earlier window exists.
 */
export function dividendPace(
  points: DividendTtmPoint[],
  coverageStart: string | null | undefined,
): DividendPace | null {
  // One window is a level, not a rate.
  if (points.length < 2) return null

  const from = points[0]
  const to = points[points.length - 1]
  const months = monthsBetween(from.month, to.month)
  // A zero base makes the rate undefined, not large — the server's `_pct` rule.
  // A negative endpoint has no real root. A `to` of exactly 0 is fine: that is
  // -100%/month, which is what a book that stopped paying looks like.
  if (months < 1 || from.total_eur <= 0 || to.total_eur < 0) return null

  const ratio = to.total_eur / from.total_eur
  const cmgr = ratio ** (1 / months) - 1

  return {
    cmgr_pct: Math.round(cmgr * 10000) / 100,
    // Only once there is a year of span to annualize from. Below that the
    // extrapolation says more about the exponent than about the portfolio:
    // seven months of a funding ramp compounds to four figures.
    cagr_pct: months >= 12 ? Math.round((ratio ** (12 / months) - 1) * 1000) / 10 : null,
    from_month: from.month,
    to_month: to.month,
    from_eur: from.total_eur,
    to_eur: to.total_eur,
    months,
    // From the anchors, never from the toggle: with the forecast shown but
    // nothing projected, both anchors are measured and neither is an estimate.
    includes_forecast: from.forecast_net_eur > 0 || to.forecast_net_eur > 0,
    coverage_limited: Boolean(coverageStart) && from.month === coverageStart,
  }
}
