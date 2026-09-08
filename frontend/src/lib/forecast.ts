import type { ContributionsResponse, PortfolioSummary } from './api'
import { cashIsTracked } from './portfolioCash'

/**
 * The Forecast tab's arithmetic — the compound-growth projection and the baseline it
 * starts from — kept pure so it tests in `node` without paying jsdom's startup, the
 * reason `portfolioKpis.ts` and `rebalance.ts` live here.
 *
 * Extracted from `ForecastTab.tsx`, where the same formula was written out four times
 * (the horizon table, the scenario cards, the sampled chart series and that series'
 * hand-copied final point), each with its own NaN guard. Two rules were each a wrong
 * number on screen before they were written down here:
 *
 * - **Money In starts at what was paid in, never at what the book is worth.** The chart's
 *   grey band used to be seeded with the current market value and then grow by the monthly
 *   contribution — so it asserted that every gain the portfolio had ever made was
 *   contributed, and the gap to the value band showed only *future* gains. The baseline is
 *   `windows['all'].money_in_eur` from `/api/portfolio/contributions`: the app's one answer
 *   to "how much have I paid in", spliced so a rotation between holdings cannot inflate it.
 *   Not cost basis, which CLAUDE.md defines as capital *deployed* and which counts a rotation
 *   twice.
 *
 * - **The three figures partition.** `value === moneyIn + gains` on every point and every
 *   row, on the *rounded* values — `gains` is the rounded remainder rather than its own
 *   rounded computation, the `fund_residual_eur` rule. So "Investment Gains" is every gain
 *   including those already made, and the gap between the two chart bands at year 0 is the
 *   book's unrealised profit today. The table and the chart used to publish two different
 *   quantities under the one name "Total Contributions", neither of them money in.
 *
 * A baseline that could not be loaded is `null`, and `moneyIn` / `gains` are `null` with
 * it. Falling back to 0 would redraw the very lie this module exists to remove; falling
 * back to the start value would too. `value` is unaffected — it never needed the baseline.
 *
 * The rate is the nominal annual figure divided by twelve and the contribution is an
 * ordinary annuity paid at each month's end — both unchanged from the original, and both
 * conventional simplifications rather than defects.
 */
export interface ForecastInputs {
  /**
   * What the account is worth today — holdings plus cash where cash is tracked, so it
   * agrees with the hero card's Total Value — or 0 when projecting from scratch.
   */
  startValue: number
  /**
   * Money paid in to date: `windows['all'].money_in_eur`; 0 when projecting from scratch;
   * `null` when the figure is not available, which is a refusal rather than a zero.
   */
  moneyInToDate: number | null
  monthlyContribution: number
  annualReturnPct: number
}

export interface ForecastPoint {
  /** Projected total value, whole units. */
  value: number
  /** Money in to date plus the contributions projected so far, or `null` when unknown. */
  moneyIn: number | null
  /** `value − moneyIn` on the rounded pair, or `null` when `moneyIn` is. */
  gains: number | null
}

export interface ForecastSample extends ForecastPoint {
  month: number
  /** `month / 12` to one decimal — the chart's category axis. */
  year: string
}

/** `Math.round`'s domain is the finite numbers; anything else has no value to show. */
function whole(v: number): number {
  return Number.isFinite(v) ? Math.round(v) : 0
}

/**
 * Value, money in and gains `months` from now.
 *
 *   value = start · (1 + r)^m + PMT · ((1 + r)^m − 1) / r      (r = 0 → PMT · m)
 */
export function projectForecast(inputs: ForecastInputs, months: number): ForecastPoint {
  const { startValue, moneyInToDate, monthlyContribution, annualReturnPct } = inputs
  const r = annualReturnPct / 100 / 12
  const growth = Math.pow(1 + r, months)
  const annuity = r === 0 ? monthlyContribution * months : monthlyContribution * ((growth - 1) / r)
  const value = whole(startValue * growth + annuity)

  if (moneyInToDate === null) return { value, moneyIn: null, gains: null }

  const moneyIn = whole(moneyInToDate + monthlyContribution * months)
  return { value, moneyIn, gains: value - moneyIn }
}

/**
 * The chart's series: one sample every `stepMonths`, plus the exact horizon as the final
 * point so the line ends where the table's row for that year says it does. The horizon is
 * appended once — the original sampled `0 … months` inclusive and then bolted on a second
 * copy of the last point whenever the step did not divide the horizon.
 */
export function forecastSeries(
  inputs: ForecastInputs,
  months: number,
  stepMonths = 6,
): ForecastSample[] {
  const samples: ForecastSample[] = []
  for (let month = 0; month < months; month += stepMonths) samples.push(sample(inputs, month))
  samples.push(sample(inputs, months))
  return samples
}

function sample(inputs: ForecastInputs, month: number): ForecastSample {
  return { month, year: (month / 12).toFixed(1), ...projectForecast(inputs, month) }
}

/**
 * Where a projection starts, from the two responses the tab reads.
 *
 * The start value is **Total Value** (holdings plus cash) whenever the backend tracks cash,
 * decided by the same `cashIsTracked` every other surface uses, and holdings alone on an
 * older backend or an account with no ledger to derive cash from. Cash has to be in for
 * the partition to hold at month 0: money in counts a deposit the moment it lands, whether
 * or not it has been invested yet, so measuring it against a holdings-only value would
 * understate the gain already made by exactly the idle balance.
 *
 * "Start from zero" zeroes both sides — it is the from-scratch scenario, and a from-scratch
 * projection that carried today's money in would report a negative gain of that size.
 *
 * `moneyInToDate` is `null`, never 0, when the contributions response is absent or carries
 * no all-time window: the reader has to distinguish "nothing paid in" from "we do not know".
 */
export function forecastBaseline(
  summary: PortfolioSummary | undefined,
  contributions: ContributionsResponse | undefined,
  startFromZero: boolean,
): Pick<ForecastInputs, 'startValue' | 'moneyInToDate'> {
  if (startFromZero) return { startValue: 0, moneyInToDate: 0 }

  const startValue =
    summary != null && cashIsTracked(summary) && summary.total_value_eur != null
      ? summary.total_value_eur
      : summary?.total_market_value_eur ?? 0

  const allTime = contributions?.windows.find((w) => w.label === 'all')
  return { startValue, moneyInToDate: allTime ? allTime.money_in_eur : null }
}
