/**
 * Target weights and the drift away from them.
 *
 * The arithmetic is trivial; the judgement is in what the function refuses to
 * say. Four rules, each of which is a wrong number the other way round:
 *
 * 1. **A missing target is not a target of zero.** An untargeted position is
 *    unmanaged, and treating its absence as 0% would advise liquidating every
 *    holding whose target has not been set yet.
 * 2. **Targets are never silently renormalised to 100%.** If they sum to 90 the
 *    drift is measured against what was actually set, and the shortfall is
 *    reported. Scaling them up invents a target the user never chose, and the
 *    invented one moves every time an unrelated target is edited.
 * 3. **An unpriced position has no weight, not a zero weight.** The portfolio
 *    values a holding with no cached price at 0.00 (see CLAUDE.md on SBI), so
 *    naive drift would advise buying its entire target — the position may in
 *    fact be the largest one held. Those rows carry `unpriced` and no advice.
 * 4. **Targets key on `security_id`, not symbol.** Security identity here is
 *    `isin + exchange`, so the same ticker legitimately appears twice — ASML
 *    trades on NASDAQ *and* AEB. A symbol-keyed target would silently apply one
 *    weight to two different holdings.
 */

import { isUnpriced } from './positionValuation'

/** Percent of the whole portfolio, keyed by `security_id`. */
export type TargetMap = Record<number, number>

export interface DriftInput {
  security_id: number
  symbol: string
  description: string
  market_value_eur: number
  market_price: number | null
}

export interface DriftRow {
  securityId: number
  symbol: string
  description: string
  marketValue: number
  /** Share of the portfolio today. Null when the position has no price. */
  currentPct: number | null
  /** What the user asked for. Null means unmanaged — never treat as 0. */
  targetPct: number | null
  /** Percentage *points* away from target: positive is overweight. */
  driftPp: number | null
  /** Base-currency trade that closes the gap: positive buys, negative sells. */
  tradeValue: number | null
  /** Null when there is no drift to judge. */
  withinBand: boolean | null
  unpriced: boolean
  /** True for a target on something not currently held. */
  unheld: boolean
}

export interface RebalancePlan {
  rows: DriftRow[]
  totalValue: number
  /** Sum of the targets actually set. 100 means fully specified. */
  targetSumPct: number
  /** Current weight sitting in positions that have a target. */
  managedPct: number
  /** …and in those that do not. The two need not sum to 100 — see `unpricedCount`. */
  unmanagedPct: number
  /** Every managed row inside its band. True when there is nothing to do. */
  balanced: boolean
  /**
   * How many rows could be judged at all — a target *and* a current weight.
   *
   * Zero is not the same as balanced, and conflating them reads as an all-clear:
   * a caller that only counts rows outside the band reports "0 outside" when in
   * fact nothing could be compared, which is what a portfolio whose positions
   * have not loaded looks like.
   */
  judgedCount: number
  /** Rows excluded from advice because the portfolio cannot value them. */
  unpricedCount: number
}

/** Default tolerance in percentage points, absolute rather than relative. */
export const DEFAULT_BAND_PP = 5

// `isUnpriced` is shared with `currencyExposure.ts` and `winRate` rather than spelled out
// here: it was two identical copies each carrying its own copy of the reasoning, and
// `winRate` would have made a third. The one-clause form both were corrected away from on
// 2026-08-05 is exactly what a drifting copy would revert to. The FX case, the false
// fully-sold premise and why this is narrower than `summary.unpriced_holdings` all live in
// `positionValuation.ts`.

export function computeRebalancePlan(
  positions: DriftInput[],
  targets: TargetMap,
  bandPp: number = DEFAULT_BAND_PP,
): RebalancePlan {
  const totalValue = positions
    .filter((p) => !isUnpriced(p))
    .reduce((s, p) => s + p.market_value_eur, 0)

  const rows: DriftRow[] = positions.map((p) => {
    const unpriced = isUnpriced(p)
    const target = targets[p.security_id]
    const targetPct = typeof target === 'number' ? target : null
    const currentPct =
      unpriced || totalValue <= 0 ? null : (p.market_value_eur / totalValue) * 100

    // Advice needs both a target and a weight to compare it against.
    const comparable = targetPct !== null && currentPct !== null
    const driftPp = comparable ? currentPct! - targetPct! : null
    const tradeValue = comparable ? (targetPct! / 100) * totalValue - p.market_value_eur : null

    return {
      securityId: p.security_id,
      symbol: p.symbol,
      description: p.description,
      marketValue: p.market_value_eur,
      currentPct,
      targetPct,
      driftPp,
      tradeValue,
      withinBand: driftPp === null ? null : Math.abs(driftPp) <= bandPp,
      unpriced,
      unheld: false,
    }
  })

  // A target on something not held yet is a buy instruction, not a missing row.
  // Dropping it would make the tab unable to express "start a position".
  const heldIds = new Set(positions.map((p) => p.security_id))
  for (const [key, targetPct] of Object.entries(targets)) {
    const securityId = Number(key)
    if (heldIds.has(securityId)) continue
    const driftPp = totalValue > 0 ? -targetPct : null
    rows.push({
      securityId,
      symbol: `#${securityId}`,
      description: 'Not currently held',
      marketValue: 0,
      currentPct: totalValue > 0 ? 0 : null,
      targetPct,
      driftPp,
      tradeValue: totalValue > 0 ? (targetPct / 100) * totalValue : null,
      withinBand: driftPp === null ? null : Math.abs(driftPp) <= bandPp,
      unpriced: false,
      unheld: true,
    })
  }

  const targetSumPct = Object.values(targets).reduce((s, v) => s + v, 0)
  const managedPct = rows
    .filter((r) => r.targetPct !== null && r.currentPct !== null)
    .reduce((s, r) => s + r.currentPct!, 0)
  const unmanagedPct = rows
    .filter((r) => r.targetPct === null && r.currentPct !== null)
    .reduce((s, r) => s + r.currentPct!, 0)

  const judged = rows.filter((r) => r.withinBand !== null)

  return {
    rows,
    totalValue,
    targetSumPct,
    managedPct,
    unmanagedPct,
    // Vacuously true with nothing targeted would read as "nothing to do" on a
    // portfolio nobody has set up yet, so an empty plan is not balanced.
    balanced: judged.length > 0 && judged.every((r) => r.withinBand),
    judgedCount: judged.length,
    unpricedCount: rows.filter((r) => r.unpriced).length,
  }
}

/** Rows worth acting on: outside the band, largest gap first. */
export function actionableRows(plan: RebalancePlan): DriftRow[] {
  return plan.rows
    .filter((r) => r.withinBand === false)
    .sort((a, b) => Math.abs(b.driftPp!) - Math.abs(a.driftPp!))
}

/**
 * How far the targets are from a fully-specified 100%.
 *
 * Reported rather than corrected. A positive number means more is targeted than
 * exists; negative means part of the portfolio has no target and the trades
 * below will not net to zero.
 */
export function targetShortfallPp(plan: RebalancePlan): number {
  return plan.targetSumPct - 100
}
