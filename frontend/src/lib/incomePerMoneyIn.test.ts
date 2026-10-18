import { describe, expect, it } from 'vitest'
import type { ContributionMonthlyItem, ContributionsResponse } from './api'
import { incomePerMoneyIn } from './incomePerMoneyIn'

function contributions(
  monthly: ContributionMonthlyItem[],
  baseCurrency = 'CHF',
): ContributionsResponse {
  return {
    windows: [], monthly, first_contribution_date: monthly[0] ? `${monthly[0].month}-01` : null,
    coverage_from: null, deposits_from: null, transfer_in_date: null, base_currency: baseCurrency,
  }
}

const month = (key: string, moneyIn: number | undefined): ContributionMonthlyItem => ({
  month: key, money_in_eur: moneyIn, deployed_eur: 0, net_eur: 0,
})

const point = (date: string, value: number) => ({ date, value })

describe('incomePerMoneyIn', () => {
  it('measures each day against the cumulative money in through the end of its month', () => {
    const result = incomePerMoneyIn(
      [
        point('2026-07-15', 30),   // 1,000 in by July
        point('2026-08-01', 50),   // August's deposit counts from the month's first day
        point('2026-09-30', 60),   // no deposit in September: August's total carries
        point('2026-10-18', 120),  // October's adds
      ],
      'CHF',
      contributions([month('2026-06', 1000), month('2026-08', 1000), month('2026-10', 2000)]),
    )
    expect(result?.values).toEqual([30, 25, 30, 30])
    expect(result?.latest).toBe(30)
    expect(result?.change).toBe(0)
    expect(result?.since).toBe('2026-07-15')
  })

  it('reads the months in date order whatever order they arrive in', () => {
    const result = incomePerMoneyIn(
      [point('2026-07-15', 30), point('2026-08-15', 60)],
      'CHF',
      contributions([month('2026-08', 1000), month('2026-06', 1000)]),
    )
    expect(result?.values).toEqual([30, 30])
  })

  it('reports the change since the first measured day, rising only when income outgrows deposits', () => {
    const result = incomePerMoneyIn(
      [point('2026-09-10', 40), point('2026-10-10', 66)],
      'CHF',
      contributions([month('2026-09', 2000), month('2026-10', 1000)]),
    )
    expect(result?.values).toEqual([20, 22])
    expect(result?.change).toBeCloseTo(2)
  })

  it('leaves a day absent while money in is not above zero, and measures the change from the first one that is', () => {
    const result = incomePerMoneyIn(
      [
        point('2026-05-20', 10),   // before the first money in
        point('2026-06-20', 20),
        point('2026-07-20', 30),   // a withdrawal took money in to zero
        point('2026-08-20', 45),
      ],
      'CHF',
      contributions([
        month('2026-06', 1000), month('2026-07', -1000), month('2026-08', 1500),
      ]),
    )
    expect(result?.values).toEqual([null, 20, null, 30])
    expect(result?.since).toBe('2026-06-20')
    expect(result?.change).toBe(10)
  })

  it('has no figure today when money in is not above zero, and so no change', () => {
    const result = incomePerMoneyIn(
      [point('2026-06-20', 20), point('2026-07-20', 30)],
      'CHF',
      contributions([month('2026-06', 1000), month('2026-07', -1200)]),
    )
    expect(result?.values).toEqual([20, null])
    expect(result?.latest).toBeNull()
    expect(result?.change).toBeNull()
  })

  it('has no change with a single measured day', () => {
    const result = incomePerMoneyIn(
      [point('2026-05-20', 10), point('2026-06-20', 20)],
      'CHF',
      contributions([month('2026-06', 1000)]),
    )
    expect(result?.latest).toBe(20)
    expect(result?.change).toBeNull()
  })

  it('is unknown, never zero, without both inputs in one currency', () => {
    const points = [point('2026-06-20', 20), point('2026-07-20', 30)]
    const ok = contributions([month('2026-06', 1000)])
    expect(incomePerMoneyIn(points, 'CHF', undefined)).toBeNull()
    // One response refetched after a base-currency switch and the other not yet.
    expect(incomePerMoneyIn(points, 'EUR', ok)).toBeNull()
    expect(incomePerMoneyIn([], 'CHF', ok)).toBeNull()
    // A backend that publishes no per-month money in.
    expect(incomePerMoneyIn(points, 'CHF', contributions([month('2026-06', undefined)]))).toBeNull()
  })

  it('has every day absent when nothing was ever put in', () => {
    const result = incomePerMoneyIn([point('2026-06-20', 20)], 'CHF', contributions([]))
    expect(result?.values).toEqual([null])
    expect(result?.latest).toBeNull()
    expect(result?.since).toBeNull()
  })
})
