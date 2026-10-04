import type { DividendAnnualRow } from './api'

/**
 * Rebuild the year rows from realized income alone, for when the Forecast toggle
 * is off.
 *
 * Leaving projected bars and "est." chips on screen while the rest of the tab has
 * dropped them makes the toggle look broken — the year panel is the one place
 * that mixes measured and projected into a single bar and a single percentage.
 *
 * The percentage is recomputed rather than reused because the backend's
 * deliberately compares net + forecast. Two rules are copied from the server
 * exactly, and are the whole reason this is a tested pure function rather than
 * inline JSX: compare only against the **immediately preceding** year (never
 * across a gap, which would report two years of growth as one), and never divide
 * by a zero base (undefined, not large).
 */
export function realizedOnlyYears(annual: DividendAnnualRow[]): DividendAnnualRow[] {
  const withIncome = annual.filter(a => a.net_eur > 0)
  return withIncome.map((row, i) => {
    const prev = i > 0 ? withIncome[i - 1] : null
    const adjacent = prev !== null && prev.year === row.year - 1
    return {
      ...row,
      forecast_net_eur: 0,
      forecast_withholding_eur: 0,
      total_eur: row.net_eur,
      yoy_pct: adjacent && prev!.net_eur > 0
        ? Math.round(((row.net_eur - prev!.net_eur) / prev!.net_eur) * 1000) / 10
        : null,
      yoy_includes_forecast: false,
      yoy_vs_partial: Boolean(adjacent && prev!.partial),
    }
  })
}
