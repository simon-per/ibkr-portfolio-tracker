import { describe, expect, it } from 'vitest'
import { cashIsTracked, inBookCash } from './portfolioCash'

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

/**
 * The client half of a pair: `test_only_a_known_positive_balance_joins_the_book` in
 * `backend/tests/test_uninvested_cash_family.py` pins `UninvestedCash.in_book` with the
 * same four cases. Allocation and Look-through divide by that; Positions divides by this.
 */
describe('inBookCash', () => {
  it('adds only a known positive balance, as UninvestedCash.in_book does', () => {
    expect(inBookCash({ amount: 100, cash_source: 'derived' })).toBe(100)
    expect(inBookCash({ amount: -250, cash_source: 'derived' })).toBe(0)
    expect(inBookCash({ amount: 0, cash_source: 'derived' })).toBe(0)
    expect(inBookCash({ amount: 0, cash_source: 'unknown' })).toBe(0)
  })

  it('adds nothing for a balance it may not present', () => {
    expect(inBookCash({ amount: 999, cash_source: 'unknown' })).toBe(0)
    expect(inBookCash({ amount: 999 })).toBe(0)
    expect(inBookCash(undefined)).toBe(0)
  })
})
