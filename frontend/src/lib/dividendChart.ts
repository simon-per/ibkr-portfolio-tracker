import type { DividendBreakdownResponse, DividendTtmPoint } from './api'
import { SERIES_IDENTITIES } from './dividendColors'

/** The fold. Not a symbol, and never present in `stack_order`. */
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
  /**
   * The rolling twelve-month rows, in the SAME key space as chartData — so one
   * `<Bar>` map draws either view and a symbol cannot be coloured differently in
   * the two.
   */
  ttmData: Record<string, number | string>[]
  /**
   * The points behind `ttmData`: same order, same length. The per-point metadata
   * (total, change, partial, provenance) stays OFF the rows deliberately — a row
   * is keyed by symbol, and a symbol is an arbitrary string that could collide
   * with any field name we invented.
   */
  ttmPoints: DividendTtmPoint[]
  /**
   * The symbols drawn as themselves, in `stack_order` — so a holding keeps its
   * vertical place in the bar as well as its colour, whichever range is showing.
   * `OTHER` is appended when anything folded.
   */
  stackSymbols: string[]
}

export interface ChartSeriesOptions {
  /** false hides projections: `ttm` drops its open windows and strips the rest. */
  showForecast?: boolean
  /**
   * Forecast-off monthly charts stop here. The server still sends the selected
   * year's full axis so Forecast can be toggled instantly; keeping later buckets
   * after hiding their bars leaves empty future month labels behind.
   */
  currentMonth?: string
  /** Overridable for tests; production always uses the palette's own count. */
  maxSeries?: number
}

/**
 * One rolling window as it reads with projections hidden.
 *
 * A window is dropped for being OPEN, never for carrying projection — those are
 * different facts, and the server's `partial` is the first. A dividend that has
 * gone ex and not paid puts projection inside a fully elapsed window, and
 * dropping that window would make twelve months of measured income vanish over
 * one unsettled payment. So the point stays and loses its forecast half, which
 * also means `total_eur` describes what is actually drawn.
 *
 * `mom_pct` compared two totals, so it is recomputed off the measured halves —
 * the sibling of `realizedOnlyYears` in dividendGrowth.ts, copying the same two
 * server rules it does: one decimal place, and a zero base yields nothing rather
 * than a percentage. The previous window is the one before it in the SERVER's
 * series, so a point keeps the same change whichever range is showing it; the
 * first point of a windowed response has no predecessor here, and reports null
 * rather than a change measured against the wrong neighbour.
 */
function withoutForecast(p: DividendTtmPoint, prev: DividendTtmPoint | undefined): DividendTtmPoint {
  if (p.forecast_net_eur === 0 && !p.mom_includes_forecast) return p
  return {
    ...p,
    forecast: {},
    forecast_net_eur: 0,
    total_eur: p.net_eur,
    mom_pct: !prev || prev.net_eur <= 0
      ? null
      : Math.round(((p.net_eur - prev.net_eur) / prev.net_eur) * 1000) / 10,
    mom_includes_forecast: false,
  }
}

/**
 * Turn the month buckets into Recharts rows.
 *
 * **Which symbols are drawn is not decided here, and that is the point.** This function
 * used to rank the buckets it had been sent and hand the top eight to the palette by
 * position, so every range produced a different ranking and a holding changed colour when
 * the range changed. The order now arrives on the response as `stack_order`, computed once
 * over the whole history and identical in every range, and this reads the first
 * `SERIES_IDENTITIES` of it.
 *
 * Two subtleties disappeared with that ranking rather than moving: scoring a symbol by its
 * widest single rolling window (a guard against summing twelve overlapping windows, which
 * ranked symbols by WHEN they paid), and ranking over the unfiltered series so the Forecast
 * toggle could not repaint. The server sees the whole history, so neither has anything left
 * to correct — see `docs/dividends.md`.
 *
 * The keys are still the buckets' own, never `securities[].symbol`: the backend disambiguates
 * a ticker spanning two instruments as "ASML (AEB)", and the two must be read from one source.
 */
export function buildChartSeries(
  data: DividendBreakdownResponse | undefined,
  { showForecast = true, currentMonth, maxSeries = SERIES_IDENTITIES }: ChartSeriesOptions = {},
): ChartSeries {
  if (!data) return { chartData: [], ttmData: [], ttmPoints: [], stackSymbols: [] }

  // `?? []` only covers a response from a backend older than this build, which one image
  // and one deploy make impossible; it folds everything into Other rather than throwing.
  // Deliberately NOT a client-side ranking fallback — a second ranking is the thing this
  // change removed, and one that runs only in an unreachable case would never be looked at.
  const top = (data.stack_order ?? []).slice(0, maxSeries)
  const identity = new Set(top)
  let hasOther = false

  /**
   * One bucket pair -> one Recharts row. Both series go through this, so the
   * fold-into-Other rule and its rounding exist exactly once.
   */
  const row = (
    month: string,
    actual: Record<string, number>,
    forecast: Record<string, number>,
  ): Record<string, number | string> => {
    const out: Record<string, number | string> = { month }
    let otherActual = 0
    let otherForecast = 0
    for (const [sym, v] of Object.entries(actual)) {
      if (identity.has(sym)) out[sym] = v
      else { otherActual += v; hasOther = true }
    }
    for (const [sym, v] of Object.entries(forecast)) {
      if (identity.has(sym)) out[FC + sym] = v
      else { otherForecast += v; hasOther = true }
    }
    if (otherActual > 0) out[OTHER] = Math.round(otherActual * 100) / 100
    if (otherForecast > 0) out[FC + OTHER] = Math.round(otherForecast * 100) / 100
    return out
  }

  const monthlyBuckets = !showForecast && currentMonth
    ? data.months.filter((m) => m.month <= currentMonth)
    : data.months
  const chartData = monthlyBuckets.map((m) => row(m.month, m.actual, m.forecast))
  // With projections hidden, a window reaching into the future would report only
  // the part of itself that has happened — so those windows are dropped, and the
  // elapsed ones that carry an unsettled payment lose their forecast half. Done
  // in one place, so the rows, the points behind them and everything derived from
  // those points (the pace, the header amount, the change chips) cannot diverge.
  const series = data.ttm_series ?? []
  const ttmPoints = showForecast
    ? series
    : series.reduce<DividendTtmPoint[]>((out, p, i) => {
      if (!p.partial) out.push(withoutForecast(p, series[i - 1]))
      return out
    }, [])
  const ttmData = ttmPoints.map((p) => row(p.month, p.actual, p.forecast))

  // `hasOther` is set by `row`, so the fold is reported only when something actually
  // folded in the months this response carries — a symbol ranked 14th that paid nothing
  // in the selected year must not put an empty Other in the legend.
  const drawn = top.filter((s) =>
    chartData.some((r) => s in r || FC + s in r) || ttmData.some((r) => s in r || FC + s in r))
  return { chartData, ttmData, ttmPoints, stackSymbols: hasOther ? [...drawn, OTHER] : drawn }
}

/**
 * Legend entries for the rows actually on screen, biggest first.
 *
 * Ordered by what the SELECTED range paid rather than by the stack, because that is the one
 * question the chart itself cannot answer, and because order and colour are independent now:
 * re-ordering the legend moves no colour. Measured and projected are summed — the legend
 * names a holding, and the bar shows the split.
 */
export function legendEntries(
  rows: Record<string, number | string>[],
  stackSymbols: string[],
): { symbol: string; total: number }[] {
  const out: { symbol: string; total: number }[] = []
  for (const symbol of stackSymbols) {
    let total = 0
    let seen = false
    for (const row of rows) {
      const a = row[symbol]
      const f = row[FC + symbol]
      if (typeof a === 'number') { total += a; seen = true }
      if (typeof f === 'number') { total += f; seen = true }
    }
    if (seen) out.push({ symbol, total })
  }
  return out.sort((a, b) => b.total - a.total)
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
