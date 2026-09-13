/**
 * Rolling risk and the drawdown ledger — the risk cards over time instead of one figure.
 *
 * Both compute from the series the page already fetched, so they add no request and cannot
 * reach Yahoo. Both reuse `portfolioKpis.ts`' own primitives (`dailyReturnSeries`, the
 * annualisation constants, the pairing rule for beta) rather than re-deriving a return: a
 * second Modified-Dietz would be the duplicated-logic failure CLAUDE.md warns about, and
 * the Sharpe drawn here on the last window must equal the card's Sharpe over that window.
 *
 * Two rules, both inherited:
 * - A metric that cannot be measured is `null`, never `0` — a `0` Sharpe or beta is a
 *   plausible answer, which is what makes it dangerous.
 * - Only the returns dated after the first valued point are walked; a pre-inception
 *   zero is measurable and zero, and seeding from it fabricates an opening fall.
 */
import {
  dailyReturnSeries,
  externalFlow,
  firstValuedPoint,
  isMeasurable,
  meanAndStdDev,
  returnsAfter,
  FLOW_EPSILON,
  MIN_PAIRED_RETURNS,
  RISK_FREE_RATE,
  TRADING_DAYS,
  type ValueSeriesPoint,
} from './portfolioKpis'

/** Twelve months of trading days. */
export const ROLLING_WINDOW_DAYS = TRADING_DAYS
/** A window with fewer usable returns than this reports nothing rather than noise. */
export const MIN_WINDOW_RETURNS = 60

export interface RollingPoint {
  date: string
  sharpe: number | null
  volatilityPct: number | null
  beta: number | null
  correlation: number | null
  /** Flow-free paired days the beta rests on; a thin window declares itself. */
  pairedDays: number
}

/** Dates on which the portfolio saw a flow — beta skips them (see `betaAndCorrelation`). */
function flowDates(series: ValueSeriesPoint[]): Set<string> {
  const out = new Set<string>()
  for (let i = 1; i < series.length; i++) {
    if (!isMeasurable(series[i - 1]) || !isMeasurable(series[i])) continue
    if (Math.abs(externalFlow(series[i - 1], series[i])) > FLOW_EPSILON) out.add(series[i].date)
  }
  return out
}

/**
 * Sharpe, volatility, beta and correlation over a trailing window, one point per day
 * once the window is full. Returns `[]` when the series never fills a window.
 */
export function rollingMetrics(
  portfolio: ValueSeriesPoint[],
  benchmark: ValueSeriesPoint[] | undefined,
  windowDays: number = ROLLING_WINDOW_DAYS,
): RollingPoint[] {
  const seed = firstValuedPoint(portfolio)
  if (seed === null) return []
  const returns = returnsAfter(seed, dailyReturnSeries(portfolio))
  if (returns.length < windowDays) return []

  const benchByDate = new Map<string, number>()
  if (benchmark) {
    for (const r of dailyReturnSeries(benchmark)) benchByDate.set(r.date, r.ret)
  }
  const skip = flowDates(portfolio)

  const out: RollingPoint[] = []
  for (let end = windowDays; end <= returns.length; end++) {
    const window = returns.slice(end - windowDays, end)
    const values = window.map((r) => r.ret)
    const { mean, stdDev } = meanAndStdDev(values)
    const annualised = stdDev * Math.sqrt(TRADING_DAYS)

    let sharpe: number | null = null
    let volatilityPct: number | null = null
    if (values.length >= MIN_WINDOW_RETURNS) {
      volatilityPct = annualised * 100
      if (annualised > 0.001) {
        sharpe = Math.max(-10, Math.min(10, (mean * TRADING_DAYS - RISK_FREE_RATE) / annualised))
      }
    }

    const p: number[] = []
    const b: number[] = []
    for (const r of window) {
      const br = benchByDate.get(r.date)
      if (br === undefined || skip.has(r.date)) continue
      p.push(r.ret)
      b.push(br)
    }
    let beta: number | null = null
    let correlation: number | null = null
    if (p.length >= MIN_PAIRED_RETURNS) {
      const mp = meanAndStdDev(p)
      const mb = meanAndStdDev(b)
      let cov = 0
      for (let i = 0; i < p.length; i++) cov += (p[i] - mp.mean) * (b[i] - mb.mean)
      cov /= p.length
      if (mb.stdDev > 0) beta = cov / (mb.stdDev * mb.stdDev)
      if (mb.stdDev > 0 && mp.stdDev > 0) correlation = cov / (mp.stdDev * mb.stdDev)
    }

    out.push({
      date: window[window.length - 1].date,
      sharpe,
      volatilityPct,
      beta,
      correlation,
      pairedDays: p.length,
    })
  }
  return out
}

export interface DrawdownEpisode {
  peakDate: string
  troughDate: string
  /** The day the index regained its peak, or `null` while the fall is still open. */
  recoveredDate: string | null
  /** Negative percent, peak to trough. */
  depthPct: number
  /** Calendar days from peak to trough. */
  daysToTrough: number
  /** Calendar days from trough back to the peak; `null` while unrecovered. */
  daysToRecover: number | null
  /** The benchmark's own move over the same peak-to-trough dates; `null` without one. */
  benchmarkPct: number | null
}

const DAY_MS = 86_400_000

function daysBetween(a: string, b: string): number {
  return Math.round((Date.parse(b) - Date.parse(a)) / DAY_MS)
}

/** Compound the benchmark's returns dated in (from, to]; null if none fall there. */
function benchmarkMove(
  benchReturns: { date: string; ret: number }[],
  from: string,
  to: string,
): number | null {
  let index = 1
  let seen = 0
  for (const r of benchReturns) {
    if (r.date > from && r.date <= to) {
      index *= 1 + r.ret
      seen++
    }
  }
  return seen > 0 ? (index - 1) * 100 : null
}

/**
 * Every fall deeper than `thresholdPct` from a running peak, walked on a flow-adjusted
 * index compounded from the portfolio's daily returns — so a deposit is not a recovery
 * and a withdrawal is not a fall. Ordered by date; the last one may still be open.
 */
export function drawdownEpisodes(
  portfolio: ValueSeriesPoint[],
  benchmark: ValueSeriesPoint[] | undefined,
  thresholdPct: number = 5,
): DrawdownEpisode[] {
  const seed = firstValuedPoint(portfolio)
  if (seed === null) return []
  const returns = returnsAfter(seed, dailyReturnSeries(portfolio))
  if (returns.length === 0) return []
  const benchReturns = benchmark ? dailyReturnSeries(benchmark) : []

  const episodes: DrawdownEpisode[] = []
  let index = 1
  let peak = 1
  let peakDate = seed.date
  let trough = 1
  let troughDate = seed.date
  let open = false

  const close = (recoveredDate: string | null) => {
    const depth = (trough / peak - 1) * 100
    if (depth <= -thresholdPct) {
      episodes.push({
        peakDate,
        troughDate,
        recoveredDate,
        depthPct: depth,
        daysToTrough: daysBetween(peakDate, troughDate),
        daysToRecover: recoveredDate ? daysBetween(troughDate, recoveredDate) : null,
        benchmarkPct: benchmarkMove(benchReturns, peakDate, troughDate),
      })
    }
    open = false
  }

  for (const r of returns) {
    index *= 1 + r.ret
    if (index >= peak) {
      if (open) close(r.date)
      peak = index
      peakDate = r.date
      trough = index
      troughDate = r.date
    } else {
      open = true
      if (index < trough) {
        trough = index
        troughDate = r.date
      }
    }
  }
  if (open) close(null)
  return episodes
}
