import { describe, expect, it } from 'vitest'
import { realizedOnlyYears } from './dividendGrowth'
import type { DividendAnnualRow } from './api'

function row(part: Partial<DividendAnnualRow> & { year: number }): DividendAnnualRow {
  return {
    net_eur: 0,
    forecast_net_eur: 0,
    total_eur: 0,
    withholding_eur: 0,
    forecast_withholding_eur: 0,
    yoy_pct: null,
    yoy_includes_forecast: false,
    yoy_vs_partial: false,
    partial: false,
    ...part,
  }
}

describe('realizedOnlyYears', () => {
  it('drops projection from the bars and the totals', () => {
    const out = realizedOnlyYears([
      row({ year: 2025, net_eur: 20, total_eur: 20 }),
      row({ year: 2026, net_eur: 30, forecast_net_eur: 40, total_eur: 70, partial: true }),
    ])
    expect(out.map(r => [r.year, r.total_eur, r.forecast_net_eur])).toEqual([
      [2025, 20, 0],
      [2026, 30, 0],
    ])
    expect(out.every(r => !r.yoy_includes_forecast)).toBe(true)
  })

  it('removes a year that had no realized income at all', () => {
    // 2027 is pure projection — with the forecast hidden it is an empty row, and
    // an empty bar labelled 2027 reads as "we earned nothing", not "not yet".
    const out = realizedOnlyYears([
      row({ year: 2026, net_eur: 30, total_eur: 30 }),
      row({ year: 2027, forecast_net_eur: 116, total_eur: 116 }),
    ])
    expect(out.map(r => r.year)).toEqual([2026])
  })

  it('recomputes growth on realized income, not on the mixed total', () => {
    // The backend compares 70 vs 20 (+250%); realized-only must compare 30 vs 20.
    const out = realizedOnlyYears([
      row({ year: 2025, net_eur: 20, total_eur: 20 }),
      row({ year: 2026, net_eur: 30, forecast_net_eur: 40, total_eur: 70, yoy_pct: 250 }),
    ])
    expect(out[1].yoy_pct).toBe(50)
  })

  it('never compares across a gap year', () => {
    // Two years of growth reported as one would be a silent overstatement — the
    // same rule the server applies.
    const out = realizedOnlyYears([
      row({ year: 2024, net_eur: 4, total_eur: 4 }),
      row({ year: 2026, net_eur: 16, total_eur: 16 }),
    ])
    expect(out[1].yoy_pct).toBeNull()
  })

  it('gives the first year no percentage', () => {
    const out = realizedOnlyYears([row({ year: 2024, net_eur: 4, total_eur: 4 })])
    expect(out[0].yoy_pct).toBeNull()
  })

  it('carries the incomplete-year caveat onto the comparison', () => {
    // 2024 holds seven months on the real account, so 2025/2024 overstates.
    const out = realizedOnlyYears([
      row({ year: 2024, net_eur: 1, total_eur: 1, partial: true }),
      row({ year: 2025, net_eur: 14, total_eur: 14 }),
    ])
    expect(out[1].yoy_pct).toBe(1300)
    expect(out[1].yoy_vs_partial).toBe(true)
  })

  it('handles an empty list', () => {
    expect(realizedOnlyYears([])).toEqual([])
  })
})

describe('realizedOnlyYears and withholding', () => {
  it('keeps the withholding received and drops the withholding projected', () => {
    const [out] = realizedOnlyYears([
      row({ year: 2026, net_eur: 100, forecast_net_eur: 50, total_eur: 150,
        withholding_eur: 15, forecast_withholding_eur: 9 }),
    ])
    expect(out.withholding_eur).toBe(15)
    expect(out.forecast_withholding_eur).toBe(0)
  })
})
