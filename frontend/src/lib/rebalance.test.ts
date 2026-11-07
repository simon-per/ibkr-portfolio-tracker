import { describe, expect, it } from 'vitest'
import {
  DEFAULT_BAND_PP,
  actionableRows,
  computeRebalancePlan,
  targetShortfallPp,
  type DriftInput,
} from './rebalance'

function pos(
  security_id: number,
  symbol: string,
  market_value_eur: number,
  market_price: number | null = 10,
): DriftInput {
  return { security_id, symbol, description: `${symbol} Inc`, market_value_eur, market_price }
}

/** No cash tracked: the book is the holdings. The cash case has its own test below. */
const UNTRACKED = { amount: 0, cash_source: 'unknown' as const }

function planOf(positions: DriftInput[], targets: Record<number, number>, band = DEFAULT_BAND_PP) {
  return computeRebalancePlan(positions, targets, band, UNTRACKED)
}

describe('the rebalance denominator (review item DR5)', () => {
  it('is the whole book, cash included, as on Positions and the Allocation tab', () => {
    const plan = computeRebalancePlan(equalFour, { 1: 25 }, DEFAULT_BAND_PP, { amount: 1000, cash_source: 'ibkr' })
    const aaa = plan.rows.find((r) => r.symbol === 'AAA')!
    expect(plan.totalValue).toBe(2000)
    expect(aaa.currentPct).toBeCloseTo(12.5, 6)
    // A 25% target of a 2000 book buys 250 more, paid from the cash.
    expect(aaa.tradeValue).toBeCloseTo(250, 6)
  })

  it('advises nothing until the cash has loaded', () => {
    const plan = computeRebalancePlan(equalFour, { 1: 25 })
    expect(plan.totalValue).toBeNull()
    expect(plan.rows.every((r) => r.currentPct === null && r.tradeValue === null)).toBe(true)
  })
})

/** Four equal positions of 250 each: 25% apiece, total 1000. */
const equalFour = [pos(1, 'AAA', 250), pos(2, 'BBB', 250), pos(3, 'CCC', 250), pos(4, 'DDD', 250)]

describe('computeRebalancePlan', () => {
  it('reports no drift when the portfolio already matches its targets', () => {
    const plan = planOf(equalFour, { 1: 25, 2: 25, 3: 25, 4: 25 })
    expect(plan.totalValue).toBe(1000)
    expect(plan.rows.every((r) => r.driftPp === 0)).toBe(true)
    expect(plan.rows.every((r) => r.tradeValue === 0)).toBe(true)
    expect(plan.balanced).toBe(true)
    expect(targetShortfallPp(plan)).toBe(0)
  })

  it('sizes the trade that closes the gap, signed to buy or sell', () => {
    // AAA is 25% but wanted at 40%: buy to 400, so +150.
    const plan = planOf(equalFour, { 1: 40, 2: 20, 3: 20, 4: 20 })
    const aaa = plan.rows.find((r) => r.symbol === 'AAA')!
    const bbb = plan.rows.find((r) => r.symbol === 'BBB')!
    expect(aaa.driftPp).toBeCloseTo(-15, 10)
    expect(aaa.tradeValue).toBeCloseTo(150, 10)
    expect(bbb.driftPp).toBeCloseTo(5, 10)
    expect(bbb.tradeValue).toBeCloseTo(-50, 10)
  })

  it('nets the trades to zero when the targets are fully specified', () => {
    // The property that makes it a rebalance rather than a contribution.
    const plan = planOf(equalFour, { 1: 40, 2: 20, 3: 20, 4: 20 })
    const net = plan.rows.reduce((s, r) => s + (r.tradeValue ?? 0), 0)
    expect(net).toBeCloseTo(0, 10)
  })

  describe('a missing target means unmanaged, never zero', () => {
    it('advises nothing on an untargeted position', () => {
      const plan = planOf(equalFour, { 1: 50, 2: 50 })
      const ccc = plan.rows.find((r) => r.symbol === 'CCC')!
      expect(ccc.targetPct).toBeNull()
      expect(ccc.driftPp).toBeNull()
      expect(ccc.tradeValue).toBeNull()
      expect(ccc.withinBand).toBeNull()
    })

    it('does not advise liquidating it', () => {
      // The bug this rule prevents: absence read as 0% would say "sell all 250".
      const plan = planOf(equalFour, { 1: 50, 2: 50 })
      const ccc = plan.rows.find((r) => r.symbol === 'CCC')!
      expect(ccc.tradeValue).not.toBe(-250)
      expect(ccc.currentPct).toBeCloseTo(25, 10)
    })

    it('splits the portfolio into managed and unmanaged weight', () => {
      const plan = planOf(equalFour, { 1: 50, 2: 50 })
      expect(plan.managedPct).toBeCloseTo(50, 10)
      expect(plan.unmanagedPct).toBeCloseTo(50, 10)
    })
  })

  describe('targets are not renormalised', () => {
    it('measures against what was set and reports the shortfall', () => {
      // 30 + 30 = 60, not 100. Renormalising would silently make each 50%.
      const plan = planOf([pos(1, 'AAA', 500), pos(2, 'BBB', 500)], { 1: 30, 2: 30 })
      expect(plan.targetSumPct).toBe(60)
      expect(targetShortfallPp(plan)).toBe(-40)
      expect(plan.rows[0].targetPct).toBe(30)
      expect(plan.rows[0].driftPp).toBeCloseTo(20, 10)
    })

    it('reports over-specified targets rather than scaling them down', () => {
      const plan = planOf(equalFour, { 1: 40, 2: 40, 3: 40, 4: 40 })
      expect(targetShortfallPp(plan)).toBe(60)
      expect(plan.rows[0].targetPct).toBe(40)
    })

    it('does not change one position\'s advice when an unrelated target is edited', () => {
      // The concrete cost of renormalising: AAA's target did not move, so its
      // advice must not either.
      const before = planOf(equalFour, { 1: 25, 2: 25 })
      const after = planOf(equalFour, { 1: 25, 2: 25, 3: 50 })
      const aaaBefore = before.rows.find((r) => r.symbol === 'AAA')!
      const aaaAfter = after.rows.find((r) => r.symbol === 'AAA')!
      expect(aaaAfter.tradeValue).toBe(aaaBefore.tradeValue)
      expect(aaaAfter.driftPp).toBe(aaaBefore.driftPp)
    })
  })

  describe('an unpriced position has no weight, not a zero weight', () => {
    const withHole = [pos(1, 'AAA', 250), pos(2, 'BBB', 250), pos(3, 'GHOST', 0, null)]

    it('gives no advice for it even when a target is set', () => {
      const plan = planOf(withHole, { 1: 40, 2: 40, 3: 20 })
      const ghost = plan.rows.find((r) => r.symbol === 'GHOST')!
      expect(ghost.unpriced).toBe(true)
      expect(ghost.currentPct).toBeNull()
      expect(ghost.driftPp).toBeNull()
      // The bug: 0% against a 20% target would advise buying the whole target,
      // when the position may in fact be the largest one held.
      expect(ghost.tradeValue).toBeNull()
    })

    it('counts it so the caller can say the total is understated', () => {
      const plan = planOf(withHole, {})
      expect(plan.unpricedCount).toBe(1)
      expect(plan.totalValue).toBe(500)
    })

    it('treats a priced position worth zero as unpriced, not as a genuine 0%', () => {
      // This used to assert the opposite, justified by "a fully-sold holding is priced
      // and really is 0%". That premise was false: get_positions_breakdown selects
      // is_open == True, so a sold-out security never appears here at all.
      //
      // What this input really represents is the second way the backend fails to value a
      // holding — the price resolved, its FX rate did not, so market_value_eur is 0 with
      // market_price still set. Frankfurter cannot serve TWD at all, so this is a live
      // shape on this account, not a contrivance. Treated as priced it took a 0% weight
      // and drift advised buying the entire target: the SBI shape by another route.
      const plan = planOf([pos(1, 'AAA', 1000), pos(2, 'NOFX', 0, 42)], { 2: 10 })
      const noFx = plan.rows.find((r) => r.symbol === 'NOFX')!
      expect(noFx.unpriced).toBe(true)
      expect(noFx.currentPct).toBeNull()
      expect(noFx.tradeValue).toBeNull()
      expect(plan.unpricedCount).toBe(1)
      // ...and it must not be counted as a judged comparison either.
      expect(plan.judgedCount).toBe(0)
    })
  })

  describe('targets key on security_id, because a symbol is not unique', () => {
    it('gives a dual-listed ticker independent targets', () => {
      // ASML on NASDAQ and on AEB are two securities sharing one symbol.
      const dual = [pos(7, 'ASML', 600), pos(8, 'ASML', 400)]
      const plan = planOf(dual, { 7: 70, 8: 30 })
      expect(plan.rows[0].targetPct).toBe(70)
      expect(plan.rows[1].targetPct).toBe(30)
      expect(plan.rows[0].driftPp).toBeCloseTo(-10, 10)
      expect(plan.rows[1].driftPp).toBeCloseTo(10, 10)
    })
  })

  describe('a target on something not held', () => {
    it('becomes a buy rather than vanishing', () => {
      const plan = planOf(equalFour, { 1: 25, 2: 25, 3: 25, 4: 15, 99: 10 })
      const fresh = plan.rows.find((r) => r.securityId === 99)!
      expect(fresh.unheld).toBe(true)
      expect(fresh.currentPct).toBe(0)
      expect(fresh.tradeValue).toBeCloseTo(100, 10)
      expect(fresh.driftPp).toBeCloseTo(-10, 10)
    })

    it('does not duplicate a held position into an unheld row', () => {
      const plan = planOf(equalFour, { 1: 25 })
      expect(plan.rows.filter((r) => r.securityId === 1)).toHaveLength(1)
    })
  })

  describe('the tolerance band', () => {
    it('treats drift inside the band as nothing to do', () => {
      const plan = planOf(equalFour, { 1: 27, 2: 23, 3: 25, 4: 25 }, 5)
      expect(plan.balanced).toBe(true)
      expect(actionableRows(plan)).toEqual([])
    })

    it('flags drift outside it and sorts by the largest gap', () => {
      const plan = planOf(equalFour, { 1: 50, 2: 10, 3: 25, 4: 15 }, 5)
      expect(plan.balanced).toBe(false)
      expect(actionableRows(plan).map((r) => r.symbol)).toEqual(['AAA', 'BBB', 'DDD'])
    })

    it('includes drift exactly on the band edge', () => {
      const plan = planOf(equalFour, { 1: 30, 2: 20, 3: 25, 4: 25 }, 5)
      expect(plan.balanced).toBe(true)
    })

    it('defaults to five percentage points', () => {
      const justOver = planOf(equalFour, { 1: 25 - (DEFAULT_BAND_PP + 1) })
      expect(justOver.balanced).toBe(false)
    })
  })

  describe('judgedCount separates "nothing to do" from "nothing to judge"', () => {
    it('counts the rows that have both a target and a weight', () => {
      const plan = planOf(equalFour, { 1: 25, 2: 25 })
      expect(plan.judgedCount).toBe(2)
    })

    it('is zero when no position could be weighed, even with targets set', () => {
      // The outage shape: targets saved, no positions loaded. Counting only rows
      // *outside* the band reports 0 here, which reads as an all-clear.
      const plan = planOf([], { 1: 30, 2: 25, 3: 20 })
      expect(plan.judgedCount).toBe(0)
      expect(plan.balanced).toBe(false)
      expect(plan.rows.filter((r) => r.withinBand === false)).toEqual([])
    })

    it('is zero when every target sits on an unpriced position', () => {
      // Same reading, with a working backend: nothing is judgeable.
      const plan = planOf([pos(1, 'GHOST', 0, null)], { 1: 100 })
      expect(plan.judgedCount).toBe(0)
      expect(plan.balanced).toBe(false)
    })
  })

  describe('degenerate inputs', () => {
    it('is not balanced when nothing has a target', () => {
      // Vacuous truth would render "nothing to do" on an unconfigured portfolio.
      const plan = planOf(equalFour, {})
      expect(plan.balanced).toBe(false)
      expect(plan.targetSumPct).toBe(0)
    })

    it('handles an empty portfolio without dividing by zero', () => {
      const plan = planOf([], { 1: 50 })
      expect(plan.totalValue).toBe(0)
      expect(plan.rows[0].currentPct).toBeNull()
      expect(plan.rows[0].tradeValue).toBeNull()
      expect(plan.balanced).toBe(false)
    })

    it('gives no weight to anything when every position is unpriced', () => {
      const plan = planOf([pos(1, 'AAA', 0, null)], { 1: 100 })
      expect(plan.totalValue).toBe(0)
      expect(plan.rows[0].currentPct).toBeNull()
    })
  })
})
