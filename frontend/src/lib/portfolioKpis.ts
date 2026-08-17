import type { BenchmarkValuePoint } from './api'
import { isValuable, type ValuablePosition } from './positionValuation'

/** Trading days per year, for annualising a daily return series. */
const TRADING_DAYS = 252
/** EUR risk-free assumption for the Sharpe numerator. */
const RISK_FREE_RATE = 0.03
/** Below this many daily returns the statistics are noise, not signal. */
const MIN_RETURNS = 5
/**
 * Below this many *paired* returns a beta is an anecdote. Higher than
 * `MIN_RETURNS` because a regression slope needs far more evidence than a mean
 * does, and because the pairing rule below throws days away.
 */
export const MIN_PAIRED_RETURNS = 20
/**
 * A cost-basis line unchanged from one day to the next subtracts to exactly
 * zero, so this only has to absorb the cent rounding the API applies.
 */
const FLOW_EPSILON = 0.01

/**
 * The minimum either series needs: a date, a value, and the cost-basis line the
 * flow is inferred from. Both `PortfolioValuePoint` and an adapted
 * `BenchmarkValuePoint` satisfy it, so the statistics below have one
 * implementation rather than a portfolio copy and a benchmark copy.
 */
export interface ValueSeriesPoint {
  date: string
  market_value_eur: number
  cost_basis_eur: number
  external_flow_eur?: number
  /** Held securities the backend could not value that day; see {@link isMeasurable}. */
  unpriced_holdings?: number
}

/**
 * Whether a point's market value is a complete sum of what was held.
 *
 * A day the backend could not fully price is not a small day — it is an unmeasured one.
 * Its `market_value_eur` omits the unpriced holdings while `cost_basis_eur` keeps them,
 * so pairing it with a neighbour manufactures a move that never happened: complete
 * 64,944 followed by fully-unpriced 0 reads as **−100% in a single day**, which then
 * becomes a real max drawdown, a real volatility, and a real Sharpe.
 *
 * That is what a stalled market-data sync looks like — not a gap in the line but a
 * smooth decay to zero, with every risk metric on the tab moving to match.
 *
 * Absent means an older backend that does not report it, read as complete: the only
 * backward-compatible choice, and the same one `externalFlow` makes.
 */
export function isMeasurable(point: ValueSeriesPoint): boolean {
  return (point.unpriced_holdings ?? 0) === 0
}

/**
 * The day's external flow: money entering (+) or leaving (−) the holdings.
 *
 * Prefers the backend's `external_flow_eur`, which values a purchase at cost and
 * a sale at its market proceeds. The fallback — the change in the cost-basis
 * line — is right for purchases but wrong for sales: cost basis falls by what
 * the lot cost while market value falls by what it sold for, so the difference
 * shows up as a fabricated return on every sale date.
 */
export function externalFlow(prev: ValueSeriesPoint, curr: ValueSeriesPoint): number {
  if (typeof curr.external_flow_eur === 'number') return curr.external_flow_eur
  return curr.cost_basis_eur - prev.cost_basis_eur
}

/**
 * Modified-Dietz daily returns, each tagged with the date it lands on.
 *
 * The dates matter because `dailyReturns` **drops** days with nothing to divide
 * by, so the nth return is not the nth calendar point — indexing back into the
 * input series to name a drawdown's peak would silently pick the wrong day.
 */
export function dailyReturnSeries(series: ValueSeriesPoint[]): { date: string; ret: number }[] {
  const out: { date: string; ret: number }[] = []
  for (let i = 1; i < series.length; i++) {
    // Either end incomplete disqualifies the pair, exactly as a flow disqualifies a day
    // for beta: excluding it costs a point and biases nothing, while keeping it invents
    // a move. See `isMeasurable`.
    if (!isMeasurable(series[i - 1]) || !isMeasurable(series[i])) continue
    const prevMV = series[i - 1].market_value_eur
    const currMV = series[i].market_value_eur
    const cf = externalFlow(series[i - 1], series[i])
    const denominator = prevMV + cf * 0.5
    if (denominator > 0) {
      out.push({ date: series[i].date, ret: (currMV - prevMV - cf) / denominator })
    }
  }
  return out
}

/**
 * Modified-Dietz daily returns: the flow is assumed to arrive mid-day, so it
 * earns half the period.
 */
export function dailyReturns(series: ValueSeriesPoint[]): number[] {
  return dailyReturnSeries(series).map((r) => r.ret)
}

/** Mean and (population) standard deviation of a sample. */
function meanAndStdDev(values: number[]): { mean: number; stdDev: number } {
  const mean = values.reduce((s, v) => s + v, 0) / values.length
  const variance = values.reduce((s, v) => s + (v - mean) ** 2, 0) / values.length
  return { mean, stdDev: Math.sqrt(variance) }
}

/**
 * Worst peak-to-trough decline of the flow-adjusted value, as a negative percent.
 *
 * `null` — never `0` — when there is not one measurable daily return to walk, because a
 * `0` there does not merely fail to inform: it *asserts* the portfolio never fell. That
 * is the reassuring-zero shape `concentrationPct` and Sharpe were both fixed for, and it
 * is worse here, since `RiskMetricsCards` reads a zero as licence to print "Never below
 * its opening value" in prose, in green.
 *
 * Reachable exactly when a stalled price feed covers the whole selected range — every
 * point unmeasurable, so `dailyReturns` drops every pair — which is the one situation
 * where "it never fell" is the last thing anyone should be told.
 */
export function maxDrawdownPct(series: ValueSeriesPoint[]): number | null {
  const returns = dailyReturns(series)
  if (returns.length === 0) return null

  let worst = 0
  let value = series.length ? series[0].market_value_eur : 0
  let peak = value
  for (const r of returns) {
    value *= 1 + r
    if (value > peak) peak = value
    if (peak > 0) {
      const drawdown = ((value - peak) / peak) * 100
      if (drawdown < worst) worst = drawdown
    }
  }
  return worst
}

/**
 * The worst drawdown *and the story around it*: when it started, when it
 * bottomed, whether it ever recovered, and how far below the running peak the
 * portfolio sits today.
 *
 * `maxDrawdownPct` answers "how bad did it get" and stops there, which reads as
 * a live warning long after the recovery. The current drawdown is the one that
 * describes now; the max describes the worst the account has survived.
 * `recoveredDate` is null while the trough is still underwater.
 *
 * **Both percentages are `null` when there is nothing measurable to walk**, and
 * `sampleDays` says how thin the window was — the shape `betaAndCorrelation` uses so a
 * refused estimate can explain itself instead of showing a confident number. A `0` here
 * is a claim ("never fell"), not an absence; see {@link maxDrawdownPct}.
 */
export function drawdownDetail(series: ValueSeriesPoint[]): {
  maxDrawdownPct: number | null
  peakDate: string | null
  troughDate: string | null
  recoveredDate: string | null
  currentDrawdownPct: number | null
  sampleDays: number
} {
  const measurable = dailyReturnSeries(series)
  if (measurable.length === 0) {
    return {
      maxDrawdownPct: null,
      peakDate: null,
      troughDate: null,
      recoveredDate: null,
      currentDrawdownPct: null,
      sampleDays: 0,
    }
  }

  let value = series.length ? series[0].market_value_eur : 0
  let peak = value
  let peakDate = series.length ? series[0].date : null

  let worst = 0
  let worstPeakDate: string | null = null
  let troughDate: string | null = null
  let recoveredDate: string | null = null
  let current = 0

  for (const { date, ret } of measurable) {
    value *= 1 + ret
    if (value >= peak) {
      // A new high closes out whatever drawdown was open. Only the *worst*
      // one's recovery is reported, and only the first time it is reached.
      if (troughDate !== null && recoveredDate === null && worst < 0) recoveredDate = date
      peak = value
      peakDate = date
    }
    if (peak > 0) {
      current = ((value - peak) / peak) * 100
      if (current < worst) {
        worst = current
        worstPeakDate = peakDate
        troughDate = date
        // A deeper trough supersedes the previous one, so its recovery is not
        // this drawdown's recovery.
        recoveredDate = null
      }
    }
  }

  return {
    maxDrawdownPct: worst,
    peakDate: worst < 0 ? worstPeakDate : null,
    troughDate: worst < 0 ? troughDate : null,
    recoveredDate,
    currentDrawdownPct: current,
    sampleDays: measurable.length,
  }
}

/**
 * Annualised volatility of the flow-adjusted daily returns, as a percent.
 *
 * Already computed inside `sharpeRatio` as its denominator and thrown away —
 * which left the portfolio reporting return per unit of risk without ever
 * reporting the risk. Null below the minimum sample rather than 0: a handful of
 * days is an unknown volatility, not a calm one.
 */
export function annualizedVolatilityPct(series: ValueSeriesPoint[]): number | null {
  const returns = dailyReturns(series)
  if (returns.length < MIN_RETURNS) return null
  return meanAndStdDev(returns).stdDev * Math.sqrt(TRADING_DAYS) * 100
}

/**
 * Annualised Sharpe of the daily return series.
 *
 * `null` — never `0` — below the minimum sample or with no measurable volatility to
 * divide by, matching {@link annualizedVolatilityPct} and {@link sortinoRatio}.
 *
 * It used to return `0`, and that was reachable in one click: selecting **MTD** in the
 * first days of a month leaves 2 or 3 daily returns, so the card rendered a green
 * `0.00` captioned "Risk-adjusted return" — an asserted, measured-looking zero — while
 * Volatility and Sortino beside it correctly showed a dash. Those two were fixed to
 * return `null` when the risk row was added; Sharpe predates them and was missed.
 *
 * A zero standing for "unknown" is the most repeated bug in this codebase, and Sharpe
 * is the worst place for it: `0.00` is a *plausible* Sharpe, so nothing about the number
 * invites doubt.
 */
export function sharpeRatio(series: ValueSeriesPoint[]): number | null {
  const returns = dailyReturns(series)
  if (returns.length < MIN_RETURNS) return null

  const { mean, stdDev } = meanAndStdDev(returns)
  const annualisedStdDev = stdDev * Math.sqrt(TRADING_DAYS)
  // Not 0 either: a return per unit of risk is undefined when there is no risk, and a
  // flat series is exactly the shape a stale price feed produces.
  if (annualisedStdDev <= 0.001) return null

  const ratio = (mean * TRADING_DAYS - RISK_FREE_RATE) / annualisedStdDev
  return Math.max(-10, Math.min(10, ratio))
}

/**
 * Sortino: the same excess return as Sharpe, divided by *downside* deviation
 * only.
 *
 * Sharpe penalises upside volatility identically to downside, so a portfolio
 * that jumps in the direction the owner wants scores as risky. The threshold is
 * the daily risk-free rate, and the deviation divides by the full sample (not
 * the count of down days), which is what keeps it comparable to Sharpe.
 *
 * Null — never a large number — when nothing fell below the threshold. A
 * portfolio with no downside has an undefined Sortino, and rendering an
 * arbitrary cap as if it were measured is how a `0` for "unknown" becomes a
 * fact elsewhere in this codebase.
 */
export function sortinoRatio(series: ValueSeriesPoint[]): number | null {
  const returns = dailyReturns(series)
  if (returns.length < MIN_RETURNS) return null

  const dailyRiskFree = RISK_FREE_RATE / TRADING_DAYS
  const downside = returns.reduce((s, r) => s + Math.min(0, r - dailyRiskFree) ** 2, 0)
  const annualisedDownside = Math.sqrt(downside / returns.length) * Math.sqrt(TRADING_DAYS)
  if (annualisedDownside <= 0.001) return null

  const mean = returns.reduce((s, r) => s + r, 0) / returns.length
  const ratio = (mean * TRADING_DAYS - RISK_FREE_RATE) / annualisedDownside
  return Math.max(-10, Math.min(10, ratio))
}

/**
 * Share of market value held in the largest N positions, as a percent.
 *
 * `null` — never `0` — when nothing is priced. A `0` here is not merely unknown, it is
 * *reassuring*: the card tones anything under 50% as good news, so an unpriced portfolio
 * rendered a green `0.0%` claiming the five largest holdings are none of the book. Its
 * neighbour `herfindahlConcentration` already returned `null` on this exact condition,
 * and the two share a card since the effective count moved into its footnote — so the
 * green zero appeared with the qualifier that would have explained it silently dropped.
 */
export function concentrationPct(
  positions: { market_value_eur: number }[],
  topN: number = 5,
): number | null {
  const total = positions.reduce((s, p) => s + p.market_value_eur, 0)
  if (total <= 0) return null
  const top = [...positions]
    .sort((a, b) => b.market_value_eur - a.market_value_eur)
    .slice(0, topN)
    .reduce((s, p) => s + p.market_value_eur, 0)
  return (top / total) * 100
}

/**
 * Share of *valued* positions sitting at a gain, plus what had to be left out.
 *
 * An unvaluable holding is excluded from **both** sides. It used to be counted on the
 * losing side of both: `get_positions_breakdown` values it at 0.00, so `gain_loss_eur`
 * becomes `-cost_basis` — a large negative — and the holding left the numerator while
 * staying in the denominator. Win Rate then read low, and its own footnote stated it as
 * fact ("36 of 39 profitable"), turning three unmeasured positions into three losses.
 * Live rather than theoretical: three newly-bought ETFs sat unpriced for hours on
 * 2026-08-07, which is ~7.7 pp on this book.
 *
 * `null` — never `0` — when nothing can be valued, for the reason spelled out on
 * {@link concentrationPct}: a 0% win rate is a plausible figure, so nothing about it
 * invites doubt. Its two neighbours in the same KPI memo already refused this condition.
 *
 * "Could be valued" comes from `positionValuation.isValuable`, shared with `rebalance.ts`
 * and `currencyExposure.ts` — three statistics that must agree about which row is
 * unvaluable, and the rule has already had to be corrected on all of them at once.
 */
export function winRate(
  positions: (ValuablePosition & { gain_loss_eur: number })[],
): { pct: number | null; profitable: number; judged: number; excluded: number } {
  const judged = positions.filter(isValuable)
  const profitable = judged.filter((p) => p.gain_loss_eur > 0).length
  return {
    pct: judged.length > 0 ? (profitable / judged.length) * 100 : null,
    profitable,
    judged: judged.length,
    excluded: positions.length - judged.length,
  }
}

/**
 * Herfindahl concentration (Σ of squared weights) and the effective number of
 * holdings it implies (1/HHI).
 *
 * A top-5 weight cannot tell 5 equal positions from 1 dominant one plus 4
 * rounding errors — both read as the same percent. The effective count can:
 * thirty-six holdings where one is half the book are worth about four.
 * Positions with no resolvable value contribute nothing rather than dragging
 * the count up, because a zero-valued row is an unpriced holding, not a small
 * one.
 */
export function herfindahlConcentration(positions: { market_value_eur: number }[]): {
  hhi: number
  effectiveHoldings: number | null
} {
  const total = positions.reduce((s, p) => s + Math.max(0, p.market_value_eur), 0)
  if (total <= 0) return { hhi: 0, effectiveHoldings: null }
  const hhi = positions.reduce(
    (s, p) => s + (Math.max(0, p.market_value_eur) / total) ** 2,
    0,
  )
  return { hhi, effectiveHoldings: hhi > 0 ? 1 / hhi : null }
}

/**
 * Reshape a benchmark point into the common series shape.
 *
 * The benchmark is a flow-matched hypothetical — the same lot cost basis
 * deployed into the index — so it shares the portfolio's cost-basis line and
 * its flows land on the same days. It carries no `external_flow_eur` of its
 * own, and its cost-basis line must **not** be used to infer one: the two
 * lines are projected into the base currency by different rules, so under a
 * non-EUR base the benchmark's moves with the exchange rate. See
 * `betaAndCorrelation`.
 */
export function benchmarkAsValueSeries(points: BenchmarkValuePoint[]): ValueSeriesPoint[] {
  return points.map((p) => ({
    date: p.date,
    market_value_eur: p.benchmark_value_eur,
    cost_basis_eur: p.cost_basis_eur,
  }))
}

/**
 * Beta and correlation of the portfolio against a benchmark, measured over the
 * days when **neither** side saw a deposit, purchase or sale.
 *
 * Flow days are dropped rather than adjusted. Modified-Dietz can net a flow out
 * of the portfolio because the backend reports what it was worth, but the
 * benchmark only exposes a cost-basis line — and on a *sale* date cost basis
 * falls by what the lot cost while value falls by what the index leg was worth,
 * which is the same asymmetry that fabricates a loss in `externalFlow`'s
 * fallback. Inventing that number would bias beta on exactly the days the
 * portfolio traded. Excluding the pair costs a few days a month and biases
 * nothing.
 *
 * **The portfolio names those days for both sides, and the benchmark's own
 * cost-basis step must not be consulted.** Both series are built from one set
 * of tax lots, so in EUR they step on exactly the same dates — but the backend
 * projects them into the base currency by different rules: the portfolio
 * converts each lot's cost at its own `open_date`, while the benchmark
 * converts its running total at *each point's* date. Under a non-EUR base that
 * makes the benchmark's line move on every day the rate moved, which reads as
 * a flow and throws the day away — on a year of CHF it left **9** usable days
 * out of the 147 the portfolio actually sat still for, so beta was permanently
 * refused. The test is therefore the portfolio's `external_flow_eur`, plus its
 * own cost-basis step for the one flow that field cannot see: a disposal whose
 * proceeds netted to zero.
 *
 * `sampleDays` is always returned, including when the estimate is refused, so a
 * thin window can say so instead of showing a confident-looking slope drawn
 * from a fortnight.
 */
export function betaAndCorrelation(
  portfolio: ValueSeriesPoint[],
  benchmark: ValueSeriesPoint[],
): { beta: number | null; correlation: number | null; sampleDays: number } {
  const byDate = new Map(benchmark.map((p) => [p.date, p]))
  const portReturns: number[] = []
  const benchReturns: number[] = []

  for (let i = 1; i < portfolio.length; i++) {
    const prev = portfolio[i - 1]
    const curr = portfolio[i]
    const benchPrev = byDate.get(prev.date)
    const benchCurr = byDate.get(curr.date)
    if (!benchPrev || !benchCurr) continue

    // This loop derives its own returns, so it needs the completeness guard too — a
    // day the portfolio could not be fully priced would otherwise regress a fabricated
    // −100% against a real benchmark move.
    if (!isMeasurable(prev) || !isMeasurable(curr)) continue

    // Either side flowing disqualifies the day for both, and the portfolio is
    // what says so — never `externalFlow(benchPrev, benchCurr)`, which measures
    // the exchange rate under a non-EUR base. See the note above.
    if (Math.abs(externalFlow(prev, curr)) > FLOW_EPSILON) continue
    if (Math.abs(curr.cost_basis_eur - prev.cost_basis_eur) > FLOW_EPSILON) continue

    if (prev.market_value_eur <= 0 || benchPrev.market_value_eur <= 0) continue
    portReturns.push(curr.market_value_eur / prev.market_value_eur - 1)
    benchReturns.push(benchCurr.market_value_eur / benchPrev.market_value_eur - 1)
  }

  const sampleDays = portReturns.length
  if (sampleDays < MIN_PAIRED_RETURNS) return { beta: null, correlation: null, sampleDays }

  const p = meanAndStdDev(portReturns)
  const b = meanAndStdDev(benchReturns)
  const covariance =
    portReturns.reduce((s, r, i) => s + (r - p.mean) * (benchReturns[i] - b.mean), 0) / sampleDays

  // A benchmark that did not move has no slope to regress against, and a
  // portfolio that did not move has no correlation — both are undefined
  // rather than zero.
  const beta = b.stdDev > 0 ? covariance / b.stdDev ** 2 : null
  const correlation =
    p.stdDev > 0 && b.stdDev > 0 ? covariance / (p.stdDev * b.stdDev) : null

  return { beta, correlation, sampleDays }
}
