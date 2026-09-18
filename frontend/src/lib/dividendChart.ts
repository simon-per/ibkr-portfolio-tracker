import type { DividendBreakdownResponse } from './api'

/** Beyond this many series the palette runs out; the rest fold into one muted bucket. */
export const MAX_SERIES = 8
export const OTHER = 'Other'
/** dataKey prefix marking a forecast series, so one stack can carry both. */
export const FC = 'f:'

const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

export function dividendMonthLabel(month: string, withYear = false): string {
  const [y, m] = month.split('-')
  const name = MONTH_NAMES[Number(m) - 1] ?? month
  return withYear ? `${name} ${y.slice(2)}` : name
}

/** The server's calendar TTM includes the ending month and eleven before it. */
export function dividendTtmWindowLabel(month: string): string {
  const [year, m] = month.split('-').map(Number)
  const start = year * 12 + m - 1 - 11
  const key = `${Math.floor(start / 12)}-${String(start % 12 + 1).padStart(2, '0')}`
  return `${dividendMonthLabel(key, true)} – ${dividendMonthLabel(month, true)}`
}

export interface ChartSeries {
  /** One row per month, keyed by series name (and FC-prefixed name for forecast). */
  chartData: Record<string, number | string>[]
  /** Stack order == palette order, so adjacent segments are the validated colour pairs. */
  stackSymbols: string[]
}

/**
 * Turn the month buckets into Recharts rows.
 *
 * Series are ranked from the buckets themselves, never from `securities[].symbol`:
 * the backend disambiguates a ticker spanning two instruments as "ASML (AEB)", so
 * ranking by bare symbol matched nothing and silently dumped both ASML series into
 * "Other". The two must be read from one source.
 */
export function buildChartSeries(
  data: DividendBreakdownResponse | undefined,
  maxSeries: number = MAX_SERIES,
): ChartSeries {
  if (!data) return { chartData: [], stackSymbols: [] }

  const totals = new Map<string, number>()
  for (const m of data.months) {
    for (const [sym, v] of [...Object.entries(m.actual), ...Object.entries(m.forecast)]) {
      totals.set(sym, (totals.get(sym) ?? 0) + v)
    }
  }

  const top = [...totals.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, maxSeries)
    .map(([s]) => s)
  // Alphabetical keeps a symbol's colour stable when switching years.
  top.sort()
  const hasOther = totals.size > top.length

  const chartData = data.months.map((m) => {
    const row: Record<string, number | string> = { month: m.month }
    let otherActual = 0
    let otherForecast = 0
    for (const [sym, v] of Object.entries(m.actual)) {
      if (top.includes(sym)) row[sym] = v
      else otherActual += v
    }
    for (const [sym, v] of Object.entries(m.forecast)) {
      if (top.includes(sym)) row[FC + sym] = v
      else otherForecast += v
    }
    if (otherActual > 0) row[OTHER] = Math.round(otherActual * 100) / 100
    if (otherForecast > 0) row[FC + OTHER] = Math.round(otherForecast * 100) / 100
    return row
  })

  return { chartData, stackSymbols: hasOther ? [...top, OTHER] : top }
}

/**
 * Symbols listed by more than one security. Only those need the venue shown in
 * the table — the common case stays uncluttered.
 */
export function duplicatedSymbols(rows: { symbol: string }[]): Set<string> {
  const seen = new Map<string, number>()
  for (const r of rows) seen.set(r.symbol, (seen.get(r.symbol) ?? 0) + 1)
  return new Set([...seen.entries()].filter(([, n]) => n > 1).map(([s]) => s))
}
