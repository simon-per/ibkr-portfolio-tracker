/**
 * The next-12-month dividend forecast per 1,000 of money in, over time (#76).
 *
 * The *Next 12 months* figure grows whenever money goes in: a deposit buys shares and the
 * shares pay. Divided by money in, the deposits come out, so this line rises only when the
 * income grows faster than what was put in — raises, a higher-yielding mix, a currency move.
 *
 * Both inputs are already in the base currency: the forecast history converts each day at
 * its own rate (`get_forecast_history`), money in each leg at its own date
 * (`get_contributions`). They are divided only when both responses name the same base
 * currency — a switch refetches them one at a time, and one currency over another is worse
 * than no figure.
 *
 * Money in is published per month, so a day is measured against the money in through the end
 * of its month: a deposit late in a month counts from that month's first snapshot, before the
 * shares it buys reach the forecast. That errs toward a dip, never toward a deposit reading as
 * growth, and it makes today's point the all-time Money In every other surface prints (the
 * server's months stop at today).
 */
import type { ContributionsResponse } from './api'

/** The unit of money in the figure is quoted per. */
export const PER_MONEY_IN = 1000

export interface IncomePerMoneyIn {
  /** One per forecast point, in its order; null where money in was not above zero. */
  values: (number | null)[]
  /** The newest point's figure; null when money in is not above zero on that day. */
  latest: number | null
  /** `latest` less the first measured point; null without two measured points. */
  change: number | null
  /** The first measured point's date, which `change` is measured from. */
  since: string | null
}

/**
 * Null when the figure is unknown: no contributions response, the two responses in different
 * currencies, a backend that publishes no per-month money in, or no forecast points.
 * `points` are the forecast's, oldest first, `date` as `YYYY-MM-DD`.
 */
export function incomePerMoneyIn(
  points: readonly { date: string; value: number }[],
  pointsCurrency: string,
  contributions: ContributionsResponse | undefined,
): IncomePerMoneyIn | null {
  if (!contributions || contributions.base_currency !== pointsCurrency || points.length === 0) {
    return null
  }
  const months: { month: string; moneyIn: number }[] = []
  for (const m of contributions.monthly) {
    // Absent means this backend does not publish it — unknown, never zero.
    if (m.money_in_eur == null) return null
    months.push({ month: m.month, moneyIn: m.money_in_eur })
  }
  months.sort((a, b) => (a.month < b.month ? -1 : a.month > b.month ? 1 : 0))

  const moneyInThrough = (month: string) => {
    let total = 0
    for (const m of months) {
      if (m.month > month) break
      total += m.moneyIn
    }
    return total
  }

  const values = points.map((p) => {
    const moneyIn = moneyInThrough(p.date.slice(0, 7))
    return moneyIn > 0 && Number.isFinite(p.value) ? (p.value / moneyIn) * PER_MONEY_IN : null
  })

  const latest = values[values.length - 1]
  const firstIndex = values.findIndex((v) => v != null)
  const first = firstIndex >= 0 ? values[firstIndex] : null
  const measured = values.filter((v) => v != null).length
  return {
    values,
    latest,
    change: latest != null && first != null && measured >= 2 ? latest - first : null,
    since: firstIndex >= 0 ? points[firstIndex].date : null,
  }
}
