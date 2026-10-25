import { describe, expect, it } from 'vitest'
import {
  COMPANY_CHARTS,
  axisTicks,
  chartRows,
  shortPeriodLabel,
  hasAnyData,
  latestValue,
  resolveCompany,
  visibleCharts,
} from './companyFinancials'
import { FIXTURE_COMPANIES, emptySeriesSet, fixtureFinancials, fixtureSeries } from './companyFinancials.fixture'

describe('chartRows', () => {
  it('keeps a null as null, never a zero', () => {
    const rows = chartRows(fixtureFinancials(), ['fcf', 'fcf_after_sbc'])
    expect(rows.map((r) => r.fcf)).toEqual([8_132e6, 3_808e6, null, 60_853e6])
    expect(rows[2]).toEqual({ period_end: '2024-01-28', label: 'FY2024', fcf: null, fcf_after_sbc: null })
  })

  it('keeps a negative value below zero', () => {
    const rows = chartRows(fixtureFinancials(), ['net_income'])
    expect(rows[1].net_income).toBe(-4_368e6)
  })

  it('merges series by period, oldest first, filling a missing period with null', () => {
    const data = fixtureFinancials({
      series: {
        fcf: { points: [{ period_end: '2025-01-26', label: 'FY2025', value: 2 }], cagr: null, yoy: null },
        fcf_after_sbc: {
          points: [
            { period_end: '2025-01-26', label: 'FY2025', value: 1 },
            { period_end: '2024-01-28', label: 'FY2024', value: 3 },
          ],
          cagr: null, yoy: null,
        },
      },
    })
    expect(chartRows(data, ['fcf', 'fcf_after_sbc'])).toEqual([
      { period_end: '2024-01-28', label: 'FY2024', fcf: null, fcf_after_sbc: 3 },
      { period_end: '2025-01-26', label: 'FY2025', fcf: 2, fcf_after_sbc: 1 },
    ])
  })
})

describe('axis ticks', () => {
  const rows = (labels: string[]) => labels.map((label, i) => ({ period_end: `2020-01-${10 + i}`, label }))
  const quarterly = (from: number, to: number) =>
    rows(Array.from({ length: (to - from + 1) * 4 }, (_, i) => `Q${(i % 4) + 1} FY${from + Math.floor(i / 4)}`))

  it('labels fiscal-year ends only, short, keeping the newest', () => {
    const ticks = axisTicks(quarterly(2016, 2025), 5)
    expect(ticks).toEqual(['Q4 FY2017', 'Q4 FY2019', 'Q4 FY2021', 'Q4 FY2023', 'Q4 FY2025'])
    expect(ticks.map(shortPeriodLabel)).toEqual(['FY17', 'FY19', 'FY21', 'FY23', 'FY25'])
  })

  it('labels every year when they fit, annual labels included', () => {
    expect(axisTicks(rows(['FY2022', 'FY2023', 'FY2024']), 5)).toEqual(['FY2022', 'FY2023', 'FY2024'])
    expect(shortPeriodLabel('FY2024')).toBe('FY24')
    expect(shortPeriodLabel('Q2 FY2024')).toBe('Q2 FY2024')
  })

  it('spaces its own periods when the window has fewer than two year ends', () => {
    expect(axisTicks(rows(['Q2 FY2025', 'Q3 FY2025', 'Q4 FY2025', 'Q1 FY2026']), 2)).toEqual(['Q3 FY2025', 'Q1 FY2026'])
    expect(axisTicks([], 5)).toEqual([])
  })
})

describe('latestValue', () => {
  it("is the newest period's value, null when that period has none", () => {
    expect(latestValue(fixtureSeries([0.1, 0.2, 0.3, null]))).toBeNull()
    expect(latestValue(fixtureSeries([0.1, 0.2, null, 0.4]))).toBe(0.4)
    expect(latestValue({ points: [], cagr: null, yoy: null })).toBeNull()
  })
})

describe('visibleCharts', () => {
  it('draws the dividend card only for a company with a dividend on record', () => {
    expect(visibleCharts(fixtureFinancials()).map((c) => c.id)).toContain('dps')
    const nonPayer = fixtureFinancials({ series: { dividends_per_share: fixtureSeries([null, null, null, null]) } })
    expect(visibleCharts(nonPayer).map((c) => c.id)).not.toContain('dps')
    expect(visibleCharts(nonPayer)).toHaveLength(COMPANY_CHARTS.length - 1)
  })
})

describe('hasAnyData', () => {
  it('is false only when no series has a value', () => {
    expect(hasAnyData(fixtureFinancials())).toBe(true)
    expect(hasAnyData(fixtureFinancials({ series: emptySeriesSet() }))).toBe(false)
  })
})

describe('resolveCompany', () => {
  it('prefers the pick, then the remembered pick, then the first company', () => {
    expect(resolveCompany(FIXTURE_COMPANIES, 22, 11)?.symbol).toBe('AMZN')
    expect(resolveCompany(FIXTURE_COMPANIES, null, 22)?.symbol).toBe('AMZN')
    expect(resolveCompany(FIXTURE_COMPANIES, null, null)?.symbol).toBe('NVDA')
  })

  it('drops a remembered company no longer in the list', () => {
    expect(resolveCompany(FIXTURE_COMPANIES, null, 999)?.symbol).toBe('NVDA')
    expect(resolveCompany([], null, 11)).toBeNull()
  })
})
