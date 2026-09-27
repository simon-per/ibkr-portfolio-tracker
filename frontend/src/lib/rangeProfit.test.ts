import { describe, expect, it } from 'vitest'
import { rangeProfitAnchorUnpriced, rangeProfitSeries, rawProfit } from './rangeProfit'
import type { PortfolioValuePoint } from './api'

function p(
  date: string,
  fields: Partial<PortfolioValuePoint> & { market_value_eur: number; cost_basis_eur: number },
): PortfolioValuePoint {
  return {
    date,
    gain_loss_eur: fields.market_value_eur - fields.cost_basis_eur,
    gain_loss_percent: 0,
    ...fields,
  }
}

describe('rangeProfitSeries', () => {
  it('starts at exactly 0 however much profit came before the range', () => {
    const data = [
      p('2026-06-01', { market_value_eur: 15000, cost_basis_eur: 10000 }),
      p('2026-06-02', { market_value_eur: 15200, cost_basis_eur: 10000 }),
    ]
    expect(rangeProfitSeries(data, false)).toEqual([0, 200])
  })

  it('uses Total Value against Money In when cash is tracked', () => {
    const data = [
      p('2026-06-01', { market_value_eur: 9000, cost_basis_eur: 8000, total_value_eur: 12000, money_in_eur: 10000 }),
      p('2026-06-02', { market_value_eur: 9000, cost_basis_eur: 8000, total_value_eur: 12500, money_in_eur: 10000 }),
    ]
    expect(rawProfit(data[0], true)).toBe(2000)
    expect(rangeProfitSeries(data, true)).toEqual([0, 500])
  })

  it('does not move on a deposit, because the baseline rises with the value', () => {
    // The reason a shift is correct here where scaling the benchmark would not have been.
    const data = [
      p('2026-06-01', { market_value_eur: 0, cost_basis_eur: 0, total_value_eur: 12000, money_in_eur: 10000 }),
      p('2026-06-02', { market_value_eur: 0, cost_basis_eur: 0, total_value_eur: 13000, money_in_eur: 11000 }),
    ]
    expect(rangeProfitSeries(data, true)).toEqual([0, 0])
  })

  it('goes negative over a losing range even while all-time profit is positive', () => {
    const data = [
      p('2026-06-01', { market_value_eur: 15000, cost_basis_eur: 10000 }),
      p('2026-06-02', { market_value_eur: 14000, cost_basis_eur: 10000 }),
    ]
    expect(rangeProfitSeries(data, false)).toEqual([0, -1000])
  })

  it('is empty for an empty range', () => {
    expect(rangeProfitSeries([], true)).toEqual([])
  })
})

describe('rangeProfitAnchorUnpriced', () => {
  it('reports the holdings the first day could not price', () => {
    const data = [
      p('2026-06-01', { market_value_eur: 1, cost_basis_eur: 1, unpriced_holdings: 2 }),
      p('2026-06-02', { market_value_eur: 1, cost_basis_eur: 1, unpriced_holdings: 0 }),
    ]
    expect(rangeProfitAnchorUnpriced(data)).toBe(2)
  })

  it('reads an absent field, or an empty range, as complete', () => {
    expect(rangeProfitAnchorUnpriced([p('2026-06-01', { market_value_eur: 1, cost_basis_eur: 1 })])).toBe(0)
    expect(rangeProfitAnchorUnpriced([])).toBe(0)
  })
})
