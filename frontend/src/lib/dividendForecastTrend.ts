/**
 * The next-12-month dividend forecast over time, from the day growth is measured from.
 *
 * The server does the arithmetic (`/api/dividends/forecast-history`: the since-start
 * percentage and the breakdown both arrive computed). What is decided here is only
 * what to show, and two of those decisions are refusals:
 *
 * 1. **Nothing before `growth_from`.** The owner named that day because the book
 *    changed shape there; drawing the days before it puts the step back on screen that
 *    the setting exists to keep out of the percentages.
 * 2. **A part too small to print is not printed.** The breakdown is a footnote to "it
 *    is growing", and on a book still being funded nearly all of it is shares bought —
 *    five figures where four round to nothing would make it the loudest thing here.
 *    `unattributed` follows the same rule, so it appears exactly when it is material.
 */
import type {
  DividendForecastChange,
  DividendForecastHistoryResponse,
} from './api'

/** Below this many units of the base currency a part is left out of the line. */
export const PART_MIN = 0.5

export type ForecastPartKey = Exclude<keyof DividendForecastChange, 'since'>

/** Display order: the cause that dominates first, the residual last. */
const PART_LABELS: [ForecastPartKey, string][] = [
  ['shares', 'shares bought'],
  ['payout', 'payouts and timing'],
  ['newly_forecast', 'newly forecast'],
  ['fx', 'currency'],
  ['unattributed', 'unexplained'],
]

export interface ForecastTrendPart {
  key: ForecastPartKey
  label: string
  value: number
}

export interface ForecastTrend {
  points: { date: string; value: number }[]
  /** The day growth is measured from: the first point drawn. */
  start: string
  latest: number
  /** Null while the series is a single day. */
  sincePct: number | null
  /** The latest day's breakdown, without the parts that round to nothing. */
  parts: ForecastTrendPart[]
  /** Set only when the breakdown starts later than the line does. */
  partsSince: string | null
}

export function partLabel(part: ForecastTrendPart): string {
  if (part.key === 'shares' && part.value < 0) return 'shares sold'
  return part.label
}

/** Null when there is nothing to draw — no history, or none from the start day on. */
export function forecastTrend(
  history: DividendForecastHistoryResponse | undefined,
): ForecastTrend | null {
  if (!history) return null
  const from = history.growth_from
  const visible = history.points.filter((p) => from == null || p.date >= from)
  if (visible.length === 0) return null

  const last = visible[visible.length - 1]
  const breakdown = last.breakdown
  const parts = breakdown
    ? PART_LABELS
        .map(([key, label]) => ({ key, label, value: breakdown[key] }))
        .filter((part) => Math.abs(part.value) >= PART_MIN)
    : []

  return {
    points: visible.map((p) => ({ date: p.date, value: p.next_12m_eur })),
    start: visible[0].date,
    latest: last.next_12m_eur,
    sincePct: last.next_12m_since_start_pct,
    parts,
    partsSince: breakdown && breakdown.since !== visible[0].date ? breakdown.since : null,
  }
}
