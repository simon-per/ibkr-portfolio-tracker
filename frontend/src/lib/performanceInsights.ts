/**
 * The Performance tab's insight tiles, computed from responses the tab already holds:
 * the range's attribution, the positions, and the dividends breakdown's `upcoming` list.
 * No request of its own, so nothing here can reach Yahoo.
 *
 * Every pick distinguishes two absences, because they render differently and mean
 * different things: `undefined` is *not loaded* (a dash), `null` is *loaded, and there is
 * no such row* ("None") — a range in which nothing lost money has no biggest drag, and a
 * dash there would claim the answer is unknown.
 */
import type { CashSource, DividendUpcomingPayment, Position, SecurityAttribution } from '@/lib/api'
import { inBookCash } from '@/lib/portfolioCash'
import { isUnpriced } from '@/lib/positionValuation'

export interface Mover {
  securityId: number
  symbol: string
  description: string
  /** The holding's P&L over the range, from the attribution — never recomputed here. */
  pnl: number
}

export interface LargestHolding {
  securityId: number
  symbol: string
  description: string
  marketValue: number
  /** Share of the account: holdings the backend could value plus in-book cash. */
  weightPct: number
}

export interface NextDividend {
  securityId: number
  symbol: string
  date: string
  amount: number
  /** The amount is derived from a gross figure under assumed withholding. */
  estimated: boolean
}

/**
 * What a position weight is a share of, so the table and the tiles cannot disagree:
 * valued holdings plus in-book cash (`inBookCash` — positive and tracked, the rule
 * Allocation and Look-through share server-side).
 */
export function weightDenominator(
  positions: readonly Position[],
  cash?: { amount: number; cash_source?: CashSource },
): number {
  const holdings = positions.reduce((sum, p) => (isUnpriced(p) ? sum : sum + p.market_value_eur), 0)
  return holdings + inBookCash(cash)
}

/**
 * The best (`'best'`) or worst (`'worst'`) contributor to the range's P&L. Only a strictly
 * positive best or strictly negative worst counts: a flat holding is neither.
 */
export function topMover(
  attributions: readonly SecurityAttribution[] | undefined,
  which: 'best' | 'worst',
): Mover | null | undefined {
  if (!attributions) return undefined
  let pick: SecurityAttribution | null = null
  for (const a of attributions) {
    if (!Number.isFinite(a.pnl_contribution_eur)) continue
    if (which === 'best' ? a.pnl_contribution_eur <= 0 : a.pnl_contribution_eur >= 0) continue
    if (
      !pick
      || (which === 'best'
        ? a.pnl_contribution_eur > pick.pnl_contribution_eur
        : a.pnl_contribution_eur < pick.pnl_contribution_eur)
    ) pick = a
  }
  return pick && {
    securityId: pick.security_id,
    symbol: pick.symbol,
    description: pick.description,
    pnl: pick.pnl_contribution_eur,
  }
}

/** The biggest holding the backend could value, by market value, with its account weight. */
export function largestHolding(
  positions: readonly Position[] | undefined,
  cash?: { amount: number; cash_source?: CashSource },
): LargestHolding | null | undefined {
  if (!positions) return undefined
  const valued = positions.filter((p) => !isUnpriced(p))
  if (valued.length === 0) return null
  const total = weightDenominator(positions, cash)
  if (total <= 0) return null
  const top = valued.reduce((a, b) => (b.market_value_eur > a.market_value_eur ? b : a))
  return {
    securityId: top.security_id,
    symbol: top.symbol,
    description: top.description,
    marketValue: top.market_value_eur,
    weightPct: (top.market_value_eur / total) * 100,
  }
}

/**
 * The next payment still to come. A `pending` row's date has passed with no cash yet —
 * owed, not next — so it is skipped, as is anything dated before `today` (`YYYY-MM-DD`).
 */
export function nextDividend(
  upcoming: readonly DividendUpcomingPayment[] | undefined,
  today: string,
): NextDividend | null | undefined {
  if (!upcoming) return undefined
  const next = upcoming
    .filter((u) => !u.pending && u.date >= today)
    .reduce<DividendUpcomingPayment | null>(
      (best, u) => (!best || u.date < best.date ? u : best),
      null,
    )
  return next && {
    securityId: next.security_id,
    symbol: next.symbol,
    date: next.date,
    amount: next.net_eur,
    estimated: next.basis === 'gross_estimate',
  }
}
