/**
 * Round axis bounds out to a "nice" step, aiming at roughly `targetCount` ticks.
 *
 * Extracted from two charts that each solved this differently. `ForecastTab` targeted a
 * tick *count* and derived the step from it; `PortfolioValueChart` used a fixed
 * 200/1000/2500/10000 ladder chosen from the data range alone — with no idea how tall
 * the chart was. That ladder is why a 20k range put eight labels into a 280px-tall
 * phone chart, roughly 35px apart.
 *
 * Targeting a count is the version that survives, because the count is the thing that
 * depends on the viewport. Unlike the ForecastTab original this handles a negative
 * minimum: the portfolio chart's profit/loss series goes below zero.
 */
export interface Axis {
  domain: [number, number]
  ticks: number[]
  /**
   * The distance between neighbouring ticks. A label needs it: `formatAxisMoney` takes
   * it to print as many decimals as the step has, so 1,200/1,250/1,300 read "1.2k,
   * 1.25k, 1.3k" rather than "1.2k, 1.3k, 1.3k".
   */
  step: number
}

/**
 * 1, 2, 2.5, 5, 10 × a power of ten — the steps that read as round numbers on an axis.
 *
 * 2.5 is in the ladder because without it the rungs are too far apart to track a target
 * count: a 20k range asked for 8 ticks and one asked for 4 both landed on a step of
 * 5000, i.e. the target was ignored. 2500 and 25000 are round for money, and the
 * PortfolioValueChart ladder this replaces had 2500 in it for exactly that reason.
 */
function niceStep(raw: number): number {
  if (!isFinite(raw) || raw <= 0) return 1
  const magnitude = Math.pow(10, Math.floor(Math.log10(raw)))
  const normalised = raw / magnitude
  const step =
    normalised <= 1 ? 1
    : normalised <= 2 ? 2
    : normalised <= 2.5 ? 2.5
    : normalised <= 5 ? 5
    : 10
  return step * magnitude
}

/**
 * @param floor Optional hard lower bound for the axis.
 *
 * Exists because rounding the minimum *out* to a step multiple is unbounded downwards,
 * and on a money axis that shows up as a large empty band under zero. Measured on the
 * real portfolio: the three visible series had a true minimum of +1,122 — nothing
 * negative anywhere — yet the caller's proportional padding (10% of a 63.9k range)
 * pushed the requested minimum to -5,268, which floored to **-20,000** against the
 * 20k step a 4-tick phone axis picks. A fifth of the plot was reserved for values that
 * did not exist and could not occur.
 *
 * The floor is applied after rounding rather than to `min`, because flooring is exactly
 * what has to be overridden: clamping the input still rounds back down to the same
 * multiple. Ticks below the floor are dropped; `domain[0]` becomes the floor itself, so
 * the lowest tick need not sit on the axis edge.
 */
export function niceTicks(
  min: number, max: number, targetCount: number, floor?: number
): Axis {
  if (!isFinite(min) || !isFinite(max) || targetCount < 1) {
    return { domain: [0, 1], ticks: [0, 1], step: 1 }
  }

  // A flat series still needs an axis with height, or every tick lands on one line.
  if (max === min) {
    const step = niceStep(Math.abs(max) / targetCount || 1)
    return applyFloor(
      { domain: [min - step, max + step], ticks: [min - step, min, max + step], step }, floor
    )
  }

  const step = niceStep((max - min) / targetCount)
  const lo = Math.floor(min / step) * step
  const hi = Math.ceil(max / step) * step

  const ticks: number[] = []
  // Accumulate by index rather than `i += step`: repeated addition of a fractional step
  // drifts, and the drift shows up as an axis label reading 2999.9999999999995.
  const count = Math.round((hi - lo) / step)
  for (let i = 0; i <= count; i++) ticks.push(lo + i * step)

  return applyFloor({ domain: [lo, hi], ticks, step }, floor)
}

/**
 * A bar chart's value axis: always through zero, which a bar is measured from.
 *
 * The bar charts left their ticks to Recharts, whose steps (350, 1,050, …) are not on
 * the ladder above and are not handed to the tick formatter, so a label could not know
 * how many decimals it needed: 0/350/700/1,050 printed "1.1k" for 1,050. Choosing the
 * ticks here is what gives the label its step.
 */
export function zeroBasedTicks(values: number[], targetCount: number): Axis {
  const finite = values.filter(Number.isFinite)
  return niceTicks(Math.min(0, ...finite), Math.max(0, ...finite), targetCount)
}

/**
 * How far below zero a money axis should be allowed to go.
 *
 * `paddedMin` is the caller's padded minimum, which on a multi-series money chart is a
 * share of the *whole* range — so the smallest series gets padded by a fraction of the
 * largest one. Two rules, in order:
 *
 *   1. Nothing negative in the data -> **zero**. No amount of padding earns a negative
 *      axis on a portfolio that has never been underwater. This is the case that was
 *      actually wrong in production: a true minimum of +1,122 reserved 20k below zero.
 *   2. Something is negative -> allow it, but stop at `cap` **unless a real value sits
 *      lower**, in which case the value wins. Clipping an actual loss off the bottom of
 *      the chart is a worse failure than a little empty space, so `cap` is deliberately
 *      soft and this function can never return a value above `minValue`.
 */
export function axisFloor(minValue: number, paddedMin: number, cap: number): number {
  if (!isFinite(minValue)) return 0
  if (minValue >= 0) return 0
  return Math.min(minValue, Math.max(paddedMin, cap))
}

function applyFloor(axis: Axis, floor?: number): Axis {
  if (floor == null || !isFinite(floor) || axis.domain[0] >= floor) return axis
  // A floor at or above the top would leave nothing to draw, so ignore it rather than
  // emit an inverted domain — recharts renders that as a blank chart.
  if (floor >= axis.domain[1]) return axis
  const ticks = axis.ticks.filter(t => t >= floor)
  return { ...axis, domain: [floor, axis.domain[1]], ticks: ticks.length ? ticks : [floor, axis.domain[1]] }
}
