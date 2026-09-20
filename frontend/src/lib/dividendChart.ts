import type { DividendBreakdownResponse, DividendTtmPoint } from './api'

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
  /** Stack order == palette order, so adjacent segments are the validated colour pairs. */
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
 * Series are ranked from the buckets themselves, never from `securities[].symbol`:
 * the backend disambiguates a ticker spanning two instruments as "ASML (AEB)", so
 * ranking by bare symbol matched nothing and silently dumped both ASML series into
 * "Other". The two must be read from one source.
 */
export function buildChartSeries(
  data: DividendBreakdownResponse | undefined,
  { showForecast = true, currentMonth, maxSeries = MAX_SERIES }: ChartSeriesOptions = {},
): ChartSeries {
  if (!data) return { chartData: [], ttmData: [], ttmPoints: [], stackSymbols: [] }

  const totals = new Map<string, number>()
  const add = (sym: string, v: number) => totals.set(sym, (totals.get(sym) ?? 0) + v)
  for (const m of data.months) {
    for (const [sym, v] of [...Object.entries(m.actual), ...Object.entries(m.forecast)]) {
      add(sym, v)
    }
  }

  // The rolling series gets a vote too, or a symbol that paid across the eleven
  // months PRECEDING the visible range — the whole of a future year's view, for
  // instance — is folded into Other on the chart that does show it.
  //
  // Its contribution is the WIDEST single window, never the sum of them. A window
  // is already a twelve-month total, so summing counts a January payment once per
  // window it falls in (twelve) and a December one once — ranking symbols by WHEN
  // they paid rather than how much, a 12x swing that would quietly reorder the
  // monthly stack too. The widest window is a year of income, counted once.
  const widest = new Map<string, number>()
  // Ranked over the UNFILTERED series: if hiding projections could change which
  // symbols hold slots, flipping the toggle would repaint the chart.
  for (const p of data.ttm_series ?? []) {
    for (const sym of new Set([...Object.keys(p.actual), ...Object.keys(p.forecast)])) {
      const v = (p.actual[sym] ?? 0) + (p.forecast[sym] ?? 0)
      if (v > (widest.get(sym) ?? 0)) widest.set(sym, v)
    }
  }
  for (const [sym, v] of widest) add(sym, v)

  const top = [...totals.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, maxSeries)
    .map(([s]) => s)
  // Alphabetical keeps a symbol's colour stable when switching years.
  top.sort()
  const hasOther = totals.size > top.length

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
      if (top.includes(sym)) out[sym] = v
      else otherActual += v
    }
    for (const [sym, v] of Object.entries(forecast)) {
      if (top.includes(sym)) out[FC + sym] = v
      else otherForecast += v
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

  return { chartData, ttmData, ttmPoints, stackSymbols: hasOther ? [...top, OTHER] : top }
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
