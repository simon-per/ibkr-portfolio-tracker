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
  /**
   * What shortened the window, written in the same spread as `partial` and `measured`:
   * edge days the backend could not fully value were trimmed off (`unpriced`), the
   * chart's range began after the period did (`range`), or both. Named because the two
   * call for different repairs — a stalled sync and the 1Y button look identical from
   * the cell — and "part of the period" alone sent the reader to the wrong one.
   */
  shortenedBy?: { unpriced: boolean; range: boolean }
}

/** The calendar period a return is labelled with, and the range it was cut from. */
export interface PeriodBounds {
  /** The labelled period's first calendar day, `YYYY-MM-DD`: `2025-09-01`, `2025-01-01`. */
  periodStart: string
  /**
   * The first day the chart's range asked for. Absent means unknown, and nothing is
   * flagged on its account — an older caller keeps the behaviour it had.
   */
  rangeStart?: string
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
 *
 * **The chart's range is the other thing that shortens a period**, and it shortened
 * them unbadged until 2026-09-12. The heatmap groups whatever points the selected
 * range contains, so on 1Y last year's "YTD" is measured from today's date to
 * 31 December and its first month from mid-month — every point in the window complete,
 * so the trim above sees nothing, while the label still names the whole period. With
 * `bounds` the period is marked partial when the range began after the period's first
 * calendar day. Compared on the *range's* start rather than the window's first point,
 * because the first trading day of a year is 2 or 3 January and must not read as a
 * truncated January.
 */
export function computeModifiedDietzReturn(
  points: PortfolioValuePoint[],
  bounds?: PeriodBounds,
): MonthReturn | null {
  let lo = 0
  let hi = points.length - 1
  while (lo <= hi && !isMeasurable(points[lo])) lo++
  while (hi > lo && !isMeasurable(points[hi])) hi--

  const trimmed = lo !== 0 || hi !== points.length - 1
  const truncated = bounds?.rangeStart !== undefined && bounds.rangeStart > bounds.periodStart
  const partial = trimmed || truncated
  const window = trimmed ? points.slice(lo, hi + 1) : points

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
      ? {
          partial: true,
          measured: { from: window[0].date, to: window[window.length - 1].date },
          shortenedBy: { unpriced: trimmed, range: truncated },
        }
      : {}),
  }
}

export interface YearRow {
  year: number
  /** January..December; `null` where the range holds no point or the return is undefined. */
  months: (MonthReturn | null)[]
  /** The year's months linked geometrically — `null` when a month inside the chain is. */
  ytd: MonthReturn | null
}

/**
 * The heatmap's matrix: one Modified Dietz return per calendar month, **measured from the
 * previous month's last point**, and a YTD that is those months compounded.
 *
 * Until 2026-10-14 each month started at its own first point, so the move from one
 * month's last close to the next month's first was in no month at all — a dozen trading
 * days a year — and the YTD column, a separate Modified Dietz over the whole year, did
 * not equal the months it sat beside (2026: months chained to about 25%, YTD read 32%).
 * Anchoring each month on the prior month-end puts every day in exactly one month, and
 * linking the months makes `∏(1 + month) = 1 + YTD` an identity rather than a hope. A
 * year-long Modified Dietz could not keep it: its flow weighting is an approximation
 * that drifts furthest over a long window with large contributions, which is this book.
 *
 * Leading months with no return (nothing held at their start) are outside the chain, and
 * the YTD says it covers part of the year; a null month *inside* the chain makes the YTD
 * null, because a compounded figure with a hole in it is not the year's return.
 */
export function monthlyReturnRows(
  data: PortfolioValuePoint[],
  rangeStart?: string,
): YearRow[] {
  if (data.length < 2) return []

  const monthGroups = new Map<string, PortfolioValuePoint[]>()
  for (const point of data) {
    const key = point.date.substring(0, 7) // "YYYY-MM"
    const group = monthGroups.get(key)
    if (group) group.push(point)
    else monthGroups.set(key, [point])
  }

  const keys = [...monthGroups.keys()].sort()
  const monthReturns = new Map<string, MonthReturn>()
  // The days each month's figure covers, whether or not it is partial — what the YTD
  // names as its own window when it is.
  const spans = new Map<string, Span>()
  let previousEnd: PortfolioValuePoint | undefined
  for (const key of keys) {
    const points = monthGroups.get(key)!
    // Not from a month-end that held nothing: the move from nothing to the first lot is
    // inception, which the first valued point already contains (`firstValuedPoint`).
    const window = previousEnd && previousEnd.market_value_eur > 0 ? [previousEnd, ...points] : points
    const result = computeModifiedDietzReturn(window, { periodStart: `${key}-01`, rangeStart })
    if (result) {
      monthReturns.set(key, result)
      spans.set(key, result.measured ?? { from: window[0].date, to: window[window.length - 1].date })
    }
    previousEnd = points[points.length - 1]
  }

  const years = [...new Set(keys.map((k) => Number(k.substring(0, 4))))]
  const rows: YearRow[] = years.map((year) => {
    const months: (MonthReturn | null)[] = new Array(12).fill(null)
    const monthSpans: (Span | null)[] = new Array(12).fill(null)
    const present: boolean[] = new Array(12).fill(false)
    for (const key of keys) {
      if (Number(key.substring(0, 4)) !== year) continue
      const i = Number(key.substring(5, 7)) - 1
      present[i] = true
      months[i] = monthReturns.get(key) ?? null
      monthSpans[i] = spans.get(key) ?? null
    }
    return { year, months, ytd: linkMonths(months, monthSpans, present, `${year}-01-01`, rangeStart) }
  })
  return rows.sort((a, b) => b.year - a.year)
}

type Span = { from: string; to: string }

function linkMonths(
  months: (MonthReturn | null)[],
  spans: (Span | null)[],
  present: boolean[],
  periodStart: string,
  rangeStart: string | undefined,
): MonthReturn | null {
  const first = months.findIndex((m) => m !== null)
  if (first < 0) return null
  let last = 11
  while (!present[last]) last--
  // A month the range holds no point in is not a hole: the next month is measured from
  // the last point before it, so its move is already inside that figure.
  const chain = months.slice(first, last + 1).filter((_, i) => present[first + i])
  if (chain.some((m) => m === null)) return null
  const linked = chain as MonthReturn[]

  const growth = linked.reduce((g, m) => g * (1 + m.returnPercent / 100), 1)
  const skippedLeading = present.slice(0, first).some(Boolean)
  const range = rangeStart !== undefined && rangeStart > periodStart
  const unpriced = linked.some((m) => m.shortenedBy?.unpriced)
  const partial = skippedLeading || range || linked.some((m) => m.partial)
  const head = linked[0]
  const tail = linked[linked.length - 1]

  return {
    returnPercent: (growth - 1) * 100,
    startValue: head.startValue,
    endValue: tail.endValue,
    newInvestment: linked.reduce((s, m) => s + m.newInvestment, 0),
    ...(partial
      ? {
          partial: true,
          measured: { from: spans[first]!.from, to: spans[last]!.to },
          shortenedBy: { unpriced, range },
        }
      : {}),
  }
}
