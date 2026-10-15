import { parseLocalDate } from './utils'

/** Up to this span a month label would repeat, so ticks name the day. 3M is ~92 days. */
export const DAY_LABEL_MAX_SPAN_DAYS = 100

export interface DateAxis {
  /** Explicit ticks, one per month's first point; absent leaves the choice to Recharts. */
  ticks?: string[]
  tickFormatter: (value: string) => string
}

/**
 * The X axis for a daily series of `YYYY-MM-DD` dates, sorted ascending.
 *
 * One copy for the value chart and the crypto chart, which had drifted: the value chart
 * printed "Aug 26" on every tick at any range, so 3M read "Aug 26, Aug 26, Sep 26".
 *
 * A month label is placed only on a month's first point, as an explicit tick list rather
 * than an empty label on the other days: Recharts thins ticks by measured label width, and
 * empty strings left it nothing to measure (the 2Y collision at 390px). Short spans get
 * Recharts' own ticks with the day named instead.
 */
export function dateAxis(dates: readonly string[]): DateAxis {
  const first = dates.length ? parseLocalDate(dates[0]) : null
  const last = dates.length ? parseLocalDate(dates[dates.length - 1]) : null
  const spanDays =
    first && last ? Math.round((last.getTime() - first.getTime()) / 86_400_000) : 0

  if (spanDays <= DAY_LABEL_MAX_SPAN_DAYS) {
    return { tickFormatter: (value) => label(value, { month: 'short', day: 'numeric' }) }
  }

  const ticks: string[] = []
  let month = ''
  for (const date of dates) {
    const key = date.substring(0, 7)
    if (key !== month) {
      ticks.push(date)
      month = key
    }
  }
  return { ticks, tickFormatter: (value) => label(value, { month: 'short', year: '2-digit' }) }
}

function label(value: string, options: Intl.DateTimeFormatOptions): string {
  // `parseLocalDate`, not `new Date(value)`: a date-only string parses as UTC midnight,
  // which west of Greenwich names the previous day — and the previous month on the 1st.
  const date = parseLocalDate(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleDateString('en-US', options)
}
