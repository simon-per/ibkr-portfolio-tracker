import { describe, expect, it } from 'vitest'
import type { ContributionsResponse, PortfolioSummary } from './api'
import { forecastBaseline, forecastSeries, projectForecast, type ForecastInputs } from './forecast'

/** A book worth more than was paid in: 17,000 of gain already made. */
const book: ForecastInputs = {
  startValue: 72_000,
  moneyInToDate: 55_000,
  monthlyContribution: 1_000,
  annualReturnPct: 8,
}

describe('projectForecast', () => {
  it('starts Money In at what was paid in, not at what the book is worth', () => {
    // The bug this module exists to fix: the chart's grey band used to start at 72,000.
    const p = projectForecast(book, 0)
    expect(p.value).toBe(72_000)
    expect(p.moneyIn).toBe(55_000)
    expect(p.gains).toBe(17_000)
  })

  it('partitions value into money in and gains at every horizon, on the rounded figures', () => {
    for (const years of [1, 5, 10, 15, 20, 30]) {
      const p = projectForecast(book, years * 12)
      expect(p.moneyIn! + p.gains!).toBe(p.value)
      expect(p.moneyIn).toBe(55_000 + 1_000 * years * 12)
      expect(Number.isInteger(p.value)).toBe(true)
    }
  })

  it('compounds the seed and the monthly contributions at the nominal rate over twelve', () => {
    const r = 0.08 / 12
    const g = (1 + r) ** 12
    expect(projectForecast(book, 12).value).toBe(Math.round(72_000 * g + 1_000 * ((g - 1) / r)))
  })

  it('is linear at a zero rate, and then the only gain is the one already made', () => {
    const p = projectForecast({ ...book, annualReturnPct: 0 }, 24)
    expect(p.value).toBe(72_000 + 24_000)
    expect(p.moneyIn).toBe(55_000 + 24_000)
    expect(p.gains).toBe(17_000)
  })

  it('reports a book worth less than was paid in as a negative gain, not a zero', () => {
    const p = projectForecast({ ...book, startValue: 50_000 }, 0)
    expect(p.gains).toBe(-5_000)
  })

  it('refuses money in and gains when the baseline is unknown, and leaves value alone', () => {
    const p = projectForecast({ ...book, moneyInToDate: null }, 60)
    expect(p.moneyIn).toBeNull()
    expect(p.gains).toBeNull()
    expect(p.value).toBe(projectForecast(book, 60).value)
  })

  it('projects from scratch with nothing baked in', () => {
    const p = projectForecast({ ...book, startValue: 0, moneyInToDate: 0 }, 12)
    expect(p.moneyIn).toBe(12_000)
    expect(p.gains).toBe(p.value - 12_000)
    expect(p.gains).toBeGreaterThan(0)
  })
})

describe('forecastSeries', () => {
  it('samples every six months and ends exactly on the horizon, once', () => {
    const s = forecastSeries(book, 120)
    expect(s.map((x) => x.month)).toEqual(Array.from({ length: 21 }, (_, i) => i * 6))
    expect(new Set(s.map((x) => x.month)).size).toBe(s.length)
  })

  it('still ends on the horizon when the step does not divide it', () => {
    expect(forecastSeries(book, 15, 6).map((x) => x.month)).toEqual([0, 6, 12, 15])
  })

  it('is the same formula the table uses — one point equals one horizon', () => {
    // The chart and the table are two readers of one function, and this is what keeps them so.
    for (const x of forecastSeries(book, 240)) {
      const { value, moneyIn, gains } = projectForecast(book, x.month)
      expect({ value: x.value, moneyIn: x.moneyIn, gains: x.gains }).toEqual({ value, moneyIn, gains })
    }
  })

  it('labels the axis in years to one decimal', () => {
    const s = forecastSeries(book, 12)
    expect(s.map((x) => x.year)).toEqual(['0.0', '0.5', '1.0'])
  })
})

describe('forecastBaseline', () => {
  const summary = (over: Partial<PortfolioSummary> = {}): PortfolioSummary => ({
    total_cost_basis_eur: 50_000,
    total_market_value_eur: 60_000,
    total_gain_loss_eur: 10_000,
    total_gain_loss_percent: 20,
    num_positions: 3,
    total_realized_gain_loss_eur: 0,
    total_realized_proceeds_eur: 0,
    total_realized_cost_basis_eur: 0,
    num_closed_positions: 0,
    ...over,
  })

  const withCash = summary({ total_cash_eur: 12_000, total_value_eur: 72_000, cash_source: 'derived' })

  const contributions = (allTimeMoneyIn?: number): ContributionsResponse => ({
    windows:
      allTimeMoneyIn === undefined
        ? []
        : [
            {
              label: 'all' as const,
              months: 24,
              partial: false,
              money_in_eur: allTimeMoneyIn,
              avg_money_in_per_month_eur: allTimeMoneyIn / 24,
              money_in_method: 'spliced' as const,
              deposits_eur: allTimeMoneyIn,
              deployed_eur: allTimeMoneyIn,
              avg_deployed_per_month_eur: allTimeMoneyIn / 24,
              net_eur: allTimeMoneyIn,
            },
          ],
    monthly: [],
    first_contribution_date: null,
    coverage_from: null,
    deposits_from: null,
    transfer_in_date: null,
    base_currency: 'CHF',
  })

  it('seeds from Total Value when cash is tracked, so the seed matches the hero card', () => {
    expect(forecastBaseline(withCash, contributions(55_000), false)).toEqual({
      startValue: 72_000,
      moneyInToDate: 55_000,
    })
  })

  it('falls back to holdings when no ledger can derive a cash balance', () => {
    const s = summary({ total_cash_eur: 0, total_value_eur: 60_000, cash_source: 'unknown' })
    expect(forecastBaseline(s, contributions(55_000), false).startValue).toBe(60_000)
  })

  it('falls back to holdings on a backend that does not send cash at all', () => {
    expect(forecastBaseline(summary(), contributions(55_000), false).startValue).toBe(60_000)
  })

  it('reads money in from the all-time window and nowhere else', () => {
    expect(forecastBaseline(withCash, contributions(55_000), false).moneyInToDate).toBe(55_000)
  })

  it('is unknown, never zero, without an all-time window or without the response', () => {
    expect(forecastBaseline(withCash, contributions(), false).moneyInToDate).toBeNull()
    expect(forecastBaseline(withCash, undefined, false).moneyInToDate).toBeNull()
  })

  it('starts from scratch on both sides, whatever the account holds', () => {
    expect(forecastBaseline(withCash, contributions(55_000), true)).toEqual({
      startValue: 0,
      moneyInToDate: 0,
    })
  })

  it('has nothing to project from without a summary, and still refuses to invent a baseline', () => {
    expect(forecastBaseline(undefined, undefined, false)).toEqual({ startValue: 0, moneyInToDate: null })
  })
})
