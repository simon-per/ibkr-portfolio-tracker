import { describe, expect, it } from 'vitest'
import { cashCaveat, cashIsTracked } from './portfolioCash'

/**
 * `cashIsTracked` decides whether four surfaces may present a cash balance as one —
 * the value chart's line pair, the summary hero card, the positions weight denominator
 * and the allocation charts. Getting it wrong in the permissive direction is the
 * expensive one: it relabels a holdings-only line "Total Value" and puts a confident
 * 0.00 cash figure on a page that has no idea.
 */
describe('cashIsTracked', () => {
  it('accepts a derived balance', () => {
    expect(cashIsTracked({ cash_source: 'derived' })).toBe(true)
  })

  it('accepts a measured balance', () => {
    expect(cashIsTracked({ cash_source: 'ibkr' })).toBe(true)
  })

  it('refuses an absent field, because an older backend is not a zero balance', () => {
    // The backward-compatible reading, and the same one `unpriced_holdings` and
    // `external_flow_eur` make about their own absence.
    expect(cashIsTracked({})).toBe(false)
    expect(cashIsTracked(undefined)).toBe(false)
    expect(cashIsTracked(null)).toBe(false)
  })

  it('refuses "unknown", which is the subtler of the two refusals', () => {
    // Here cash really does compute to 0.00 everywhere — and that is exactly why it
    // must not be shown. No ledger holds a row, so the zero is the absence of a
    // measurement rather than a measurement of absence.
    expect(cashIsTracked({ cash_source: 'unknown' })).toBe(false)
  })
})

describe('cashCaveat', () => {
  it('qualifies a derived balance, because it is ours rather than the broker\'s', () => {
    const caveat = cashCaveat('derived')
    expect(caveat).toBeTruthy()
    // The three things the derivation structurally cannot see are what the sentence is
    // for; a caveat that does not say what is missing is decoration.
    expect(caveat).toMatch(/interest/)
  })

  it('leaves a measured balance unqualified', () => {
    // IBKR's own end-of-day figure already includes the fees and interest, so a
    // qualifier here would tell the reader to distrust the authoritative number.
    expect(cashCaveat('ibkr')).toBeNull()
  })

  it('leaves an untracked balance unqualified', () => {
    // Nothing is on screen to qualify — `cashIsTracked` has already refused it.
    expect(cashCaveat('unknown')).toBeNull()
    expect(cashCaveat(undefined)).toBeNull()
  })
})
