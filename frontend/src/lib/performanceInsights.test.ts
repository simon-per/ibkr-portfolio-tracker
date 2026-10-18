import { describe, expect, it } from 'vitest'
import type { DividendUpcomingPayment, Position, SecurityAttribution } from '@/lib/api'
import { largestHolding, nextDividend, topMover, weightDenominator } from './performanceInsights'

/** Made-up figures throughout. */
function attr(security_id: number, symbol: string, pnl: number): SecurityAttribution {
  return {
    security_id, symbol, description: `${symbol} Corp`,
    start_market_value_eur: 100, end_market_value_eur: 100 + pnl, new_investment_eur: 0,
    value_change_eur: pnl, pnl_contribution_eur: pnl, contribution_percent: 0, weight_percent: 0,
  }
}

function pos(security_id: number, symbol: string, value: number, price: number | null = 10): Position {
  return {
    security_id, symbol, description: `${symbol} Corp`, isin: `X${security_id}`, currency: 'USD',
    exchange: 'NYSE', quantity: 1, cost_basis_eur: 50, market_value_eur: value,
    market_price: price, gain_loss_eur: value - 50, gain_loss_percent: 0, taxlots: [],
    analyst_rating: null,
  }
}

function pay(over: Partial<DividendUpcomingPayment> & { date: string; symbol: string }): DividendUpcomingPayment {
  return {
    ex_date: null, security_id: 1, net_eur: 5, basis: 'net', pay_date_source: 'accrual',
    amount_source: null, pending: false, ...over,
  }
}

describe('topMover', () => {
  const rows = [attr(1, 'AAA', 40), attr(2, 'BBB', -15), attr(3, 'CCC', 120), attr(4, 'DDD', -60)]

  it('picks the largest gain and the largest loss', () => {
    expect(topMover(rows, 'best')?.symbol).toBe('CCC')
    expect(topMover(rows, 'best')?.pnl).toBe(120)
    expect(topMover(rows, 'worst')?.symbol).toBe('DDD')
  })

  it('separates not loaded from nothing to report', () => {
    expect(topMover(undefined, 'best')).toBeUndefined()
    // Nothing lost money: there is no biggest drag, and that is an answer, not a gap.
    expect(topMover([attr(1, 'AAA', 10)], 'worst')).toBeNull()
    expect(topMover([attr(1, 'AAA', -10)], 'best')).toBeNull()
  })

  it('counts a flat holding as neither', () => {
    expect(topMover([attr(1, 'AAA', 0)], 'best')).toBeNull()
    expect(topMover([attr(1, 'AAA', 0)], 'worst')).toBeNull()
  })
})

describe('largestHolding', () => {
  it('weights against valued holdings plus tracked cash', () => {
    const top = largestHolding(
      [pos(1, 'AAA', 300), pos(2, 'BBB', 500)],
      { amount: 200, cash_source: 'ibkr' },
    )
    expect(top?.symbol).toBe('BBB')
    expect(top?.weightPct).toBeCloseTo(50)
  })

  it('adds only a positive balance, as Allocation and Look-through do (GitHub #52)', () => {
    // Holdings 900: a 90 holding is 10.0% beside a debit (left out, not subtracted) and
    // 9.0% beside a credit — the same reading `UninvestedCash.in_book` gives server-side.
    const rows = [pos(1, 'BIG', 810), pos(2, 'SMALL', 90)]
    expect(weightDenominator(rows, { amount: -100, cash_source: 'derived' })).toBe(900)
    expect((90 / weightDenominator(rows, { amount: -100, cash_source: 'ibkr' })) * 100).toBeCloseTo(10)
    expect((90 / weightDenominator(rows, { amount: 100, cash_source: 'derived' })) * 100).toBeCloseTo(9)
    // The Largest holding tile reads the same denominator.
    expect(largestHolding(rows, { amount: -100, cash_source: 'derived' })?.weightPct).toBeCloseTo(90)
  })

  it('ignores cash the backend does not track', () => {
    const top = largestHolding([pos(1, 'AAA', 300), pos(2, 'BBB', 100)], { amount: 999, cash_source: 'unknown' })
    expect(top?.weightPct).toBeCloseTo(75)
  })

  it('never crowns, nor divides by, an unvaluable holding', () => {
    // An FX failure keeps the price and zeroes the value; a missing price clears both.
    const rows = [pos(1, 'AAA', 100), pos(2, 'NOFX', 0, 42), pos(3, 'NOPX', 9999, null)]
    expect(largestHolding(rows)?.symbol).toBe('AAA')
    expect(largestHolding(rows)?.weightPct).toBeCloseTo(100)
    expect(weightDenominator(rows)).toBe(100)
  })

  it('separates not loaded from nothing valued', () => {
    expect(largestHolding(undefined)).toBeUndefined()
    expect(largestHolding([])).toBeNull()
    expect(largestHolding([pos(1, 'AAA', 100, null)])).toBeNull()
  })
})

describe('nextDividend', () => {
  const TODAY = '2026-05-10'

  it('takes the earliest payment still to come', () => {
    const next = nextDividend([
      pay({ date: '2026-06-01', symbol: 'LATE' }),
      pay({ date: '2026-05-12', symbol: 'SOON', net_eur: 7.5 }),
    ], TODAY)
    expect(next).toMatchObject({ symbol: 'SOON', date: '2026-05-12', amount: 7.5, estimated: false })
  })

  it('skips a pending payment and anything already past', () => {
    const next = nextDividend([
      pay({ date: '2026-05-01', symbol: 'OWED', pending: true }),
      pay({ date: '2026-05-09', symbol: 'PAST' }),
      pay({ date: '2026-05-10', symbol: 'TODAY' }),
    ], TODAY)
    expect(next?.symbol).toBe('TODAY')
  })

  it('marks an amount derived from gross as estimated', () => {
    expect(nextDividend([pay({ date: '2026-05-20', symbol: 'G', basis: 'gross_estimate' })], TODAY)?.estimated)
      .toBe(true)
  })

  it('separates not loaded from nothing scheduled', () => {
    expect(nextDividend(undefined, TODAY)).toBeUndefined()
    expect(nextDividend([], TODAY)).toBeNull()
    expect(nextDividend([pay({ date: '2026-05-01', symbol: 'OWED', pending: true })], TODAY)).toBeNull()
  })
})
