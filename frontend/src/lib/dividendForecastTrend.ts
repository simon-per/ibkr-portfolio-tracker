/**
 * The next-12-month dividend forecast over time, from the day growth is measured from.
 *
 * The server does the arithmetic (`/api/dividends/forecast-history`: the percentages and
 * the breakdown arrive computed). What is decided here is what to show, and three of
 * those decisions are refusals or restatements:
 *
 * 1. **Nothing before `growth_from`.** The owner named that day because the book
 *    changed shape there; drawing the days before it puts the step back on screen that
 *    the setting exists to keep out of the percentages.
 * 2. **A part too small to print is not printed.** The breakdown is a footnote to "it
 *    is growing", and on a book still being funded nearly all of it is shares bought —
 *    five figures where four round to nothing would make it the loudest thing here.
 *    `unattributed` follows the same rule, so it appears exactly when it is material.
 * 3. **Today is the live figure, not the last snapshot.** The KPI tile above shows
 *    `growth.next_12m_eur` from the breakdown, recomputed on every read; the newest
 *    snapshot is whatever the last repricing job stored. Under one label they showed two
 *    values whenever the forecast had moved since (a WHT change, a new UTC day, a
 *    stopped scheduler). So the live figure is today's point: it replaces the newest
 *    snapshot when that is today's (the server keys rows on the UTC date and the last
 *    write wins, so it is what the next write would say) and is appended otherwise. The
 *    header's percentages are then re-applied to it from the same anchor days the
 *    server named — `growthPct` is the client's copy of `_pct` — so the figure and its
 *    chips cannot disagree either.
 */
import type {
  DividendForecastChange,
  DividendForecastHistoryResponse,
} from './api'
import { growthPct } from './dividendGrowth'

/** Below this many units of the base currency a part is left out of the line. */
export const PART_MIN = 0.5

/** The history's percentages carry two decimals (`_pct(..., digits=2)`). */
const HISTORY_DIGITS = 2

export type ForecastPartKey = Exclude<keyof DividendForecastChange, 'since'>

/**
 * Display order: the cause that dominates first, the residual last. `shares` is not
 * here: it is the one lever the owner controls, so it rides in the header as `fromBuys`.
 */
const PART_LABELS: [ForecastPartKey, string][] = [
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

/** Today's figures off the breakdown, in its base currency. */
export interface LiveForecast {
  /** `growth.next_12m_eur` — the KPI tile's figure. */
  next12m: number
  /** `growth.ttm.net_eur` — the "Last 12 months" tile's figure. */
  ttm: number
  currency: string
}

export interface ForecastTrendPoint {
  date: string
  value: number
  /** Income received over the trailing twelve months on that day. */
  received: number
}

export interface ForecastTrend {
  points: ForecastTrendPoint[]
  /** The day growth is measured from: the first point drawn. */
  start: string
  latest: number
  /** Null while the series is a single day. */
  sincePct: number | null
  /** Since the month / the year began; null on a day that is its own start. */
  mtdPct: number | null
  ytdPct: number | null
  /** The next-12-month income added by shares bought since `partsSince`; null when the
   *  breakdown is absent or rounds to nothing. Negative when shares were sold. */
  fromBuys: number | null
  /** The latest day's other parts, without the ones that round to nothing. */
  parts: ForecastTrendPart[]
  /** Set only when the breakdown starts later than the line does. */
  partsSince: string | null
}

/** "from buys", or "from sales" when the share count fell. */
export function fromBuysLabel(value: number): string {
  return value < 0 ? 'from sales' : 'from buys'
}

/** The UTC calendar date — the key the server stores one snapshot per. */
export function utcToday(now: Date = new Date()): string {
  return now.toISOString().slice(0, 10)
}

const sameMonth = (a: string, b: string) => a.slice(0, 7) === b.slice(0, 7)
const sameYear = (a: string, b: string) => a.slice(0, 4) === b.slice(0, 4)

/**
 * Null when there is nothing to draw — no history, or none from the start day on.
 *
 * `live` is ignored when it is in another currency than the history (one refetched
 * after a base-currency switch and the other not yet): two currencies on one line is
 * worse than a stale point.
 */
export function forecastTrend(
  history: DividendForecastHistoryResponse | undefined,
  live?: LiveForecast | null,
  today: string = utcToday(),
): ForecastTrend | null {
  if (!history) return null
  const from = history.growth_from
  const visible = history.points.filter((p) => from == null || p.date >= from)
  if (visible.length === 0) return null

  const newest = visible[visible.length - 1]
  const breakdown = newest.breakdown
  const parts = breakdown
    ? PART_LABELS
        .map(([key, label]) => ({ key, label, value: breakdown[key] }))
        .filter((part) => Math.abs(part.value) >= PART_MIN)
    : []
  const shares = breakdown?.shares
  const common = {
    start: visible[0].date,
    fromBuys: shares != null && Math.abs(shares) >= PART_MIN ? shares : null,
    parts,
    partsSince: breakdown && breakdown.since !== visible[0].date ? breakdown.since : null,
  }
  const drawn = visible.map((p) => ({
    date: p.date, value: p.next_12m_eur, received: p.ttm_net_eur,
  }))

  if (!live || live.currency !== history.base_currency) {
    return {
      ...common,
      points: drawn,
      latest: newest.next_12m_eur,
      sincePct: newest.next_12m_since_start_pct,
      mtdPct: newest.next_12m_mtd_pct,
      ytdPct: newest.next_12m_ytd_pct,
    }
  }

  // A newest row dated today (or later, on a client clock running behind UTC) is the
  // row the next write would replace; anything older is a previous day.
  const replaces = newest.date >= today
  const now = { date: replaces ? newest.date : today, value: live.next12m, received: live.ttm }
  const points = replaces ? [...drawn.slice(0, -1), now] : [...drawn, now]

  const valueOn = new Map(visible.map((p) => [p.date, p.next_12m_eur]))
  const pctSince = (anchor: string | null) =>
    anchor == null ? null : growthPct(live.next12m, valueOn.get(anchor), HISTORY_DIGITS)
  // The server's rule for a period: measured from the last snapshot before it began,
  // else from its first snapshot inside it; a day that is its own anchor has none.
  // Replacing the newest row keeps that row's anchor. Appending one in the same period
  // inherits it too — or, when the newest row was its own anchor, measures from that
  // row. Appending into a new period measures from the newest row, which is then the
  // last snapshot before the period began.
  const anchor = (named: string | null, same: (a: string, b: string) => boolean) => {
    if (replaces) return named
    if (!same(newest.date, today)) return newest.date
    return named ?? newest.date
  }

  return {
    ...common,
    points,
    latest: live.next12m,
    sincePct: pctSince(replaces && visible.length === 1 ? null : visible[0].date),
    mtdPct: pctSince(anchor(newest.mtd_from, sameMonth)),
    ytdPct: pctSince(anchor(newest.ytd_from, sameYear)),
  }
}
