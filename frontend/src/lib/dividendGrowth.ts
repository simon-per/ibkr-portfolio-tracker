import type { DividendAnnualRow, DividendRaise } from './api'
import { deltaTone } from './delta'

/**
 * Growth of `current` over `base` in percent, or null when there is nothing to grow from.
 *
 * The client's one copy of the server's `DividendService._pct`, and both of its rules:
 * a zero (or negative) base yields null — undefined, not large — and the result is
 * rounded to `digits` decimal places (1 for the breakdown's growth, 2 for the forecast
 * history's). Every client-side dividend percentage goes through here, so the rules are
 * written down once on this side of the language boundary.
 */
export function growthPct(current: number, base: number | null | undefined, digits = 1): number | null {
  if (base == null || !(base > 0)) return null
  return Math.round(((current - base) / base) * 10 ** (digits + 2)) / 10 ** digits
}

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
      yoy_pct: adjacent ? growthPct(row.net_eur, prev!.net_eur) : null,
      yoy_includes_forecast: false,
      yoy_vs_partial: Boolean(adjacent && prev!.partial),
    }
  })
}

/**
 * A payer's per-share growth inside this band, either way, is neither a raise nor a cut.
 *
 * One percent, because the figure is measured in each payer's own currency off the
 * rounded per-share rates IBKR and Yahoo state — a "0.24 → 0.2401" restatement is not a
 * raise — while a real raise is almost never under a few percent.
 *
 * **The one definition, app-wide.** The server serves signed percentages and classifies
 * nothing (`tests/test_dividend_raises_feed.py` refuses a band constant under `app/`), so
 * the "raised · cut" count, the Growth column's tone, the calendar's "+x%" tag and the
 * raises feed all decide here, through `payoutVerdict`.
 */
export const RAISE_BAND_PCT = 1

export type PayoutVerdict = 'raised' | 'cut' | 'flat'

/**
 * Whether a per-share change is a raise, a cut or neither, at `RAISE_BAND_PCT` — the
 * band's edge inside it, as `deltaTone` puts it, so the Growth column's colour (which
 * reads `deltaColor` at the same band) agrees with every count. Null without a figure:
 * no comparison, which says nothing about whether the dividend moved.
 */
export function payoutVerdict(pct: number | null | undefined): PayoutVerdict | null {
  if (pct == null || !Number.isFinite(pct)) return null
  const tone = deltaTone(pct, RAISE_BAND_PCT)
  return tone === 'up' ? 'raised' : tone === 'down' ? 'cut' : 'flat'
}

/**
 * The raises feed as shown: the server's moves that are a raise or a cut, in its order
 * (newest first). A move inside the band is a restated rate, not news.
 */
export function feedRaises(raises: readonly DividendRaise[] | null | undefined): DividendRaise[] {
  return (raises ?? []).filter((r) => {
    const verdict = payoutVerdict(r.change_pct)
    return verdict === 'raised' || verdict === 'cut'
  })
}

export interface PayoutChanges {
  raised: number
  cut: number
  /** Inside the band. Counted so the three add up to `measured`, not rendered. */
  flat: number
  /** Payers with a per-share growth figure at all. */
  measured: number
}

/**
 * How many payers raised and how many cut, from each row's own per-share growth.
 *
 * A row whose growth is null is left out, not counted as flat: null means there is no
 * payment a year back to compare with, which says nothing about whether the dividend
 * moved. Null overall when no payer is measured — "0 raised · 0 cut" over nothing would
 * read as a book whose payers all held still.
 */
export function payoutChanges(
  rows: readonly { per_share_growth_pct?: number | null }[],
): PayoutChanges | null {
  const out: PayoutChanges = { raised: 0, cut: 0, flat: 0, measured: 0 }
  for (const row of rows) {
    const verdict = payoutVerdict(row.per_share_growth_pct)
    if (verdict === null) continue
    out.measured += 1
    out[verdict] += 1
  }
  return out.measured > 0 ? out : null
}
