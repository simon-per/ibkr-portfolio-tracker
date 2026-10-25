import { describe, expect, it } from 'vitest'
import {
  COMPANY_CHARTS,
  axisTicks,
  chartRows,
  chartView,
  companyGroups,
  shortPeriodLabel,
  hasAnyData,
  latestValue,
  resolveCompany,
  seriesWithData,
  visibleCharts,
  visibleKeyStats,
} from './companyFinancials'
import {
  AAPL_CIK, AMZN_CIK, FIXTURE_COMPANIES, FIXTURE_KEY_STATS, NVDA_CIK, emptySeriesSet, fixtureFinancials,
  fixtureSeries,
} from './companyFinancials.fixture'

describe('chartRows', () => {
  it('keeps a null as null, never a zero', () => {
    const rows = chartRows(fixtureFinancials(), ['fcf', 'fcf_after_sbc'])
    expect(rows.map((r) => r.fcf)).toEqual([8_132e6, 3_808e6, null, 60_853e6])
    expect(rows[2]).toEqual({
      period_end: '2024-01-28', label: 'FY2024', fcf: null, fcf_after_sbc: null,
      yoy: { fcf: null, fcf_after_sbc: null },
    })
  })

  it("carries each point's own YoY from the server", () => {
    const rows = chartRows(fixtureFinancials(), ['revenue'])
    expect(rows.map((r) => r.yoy.revenue)).toEqual([null, null, null, 1.142])
  })

  it('keeps a negative value below zero', () => {
    const rows = chartRows(fixtureFinancials(), ['net_income'])
    expect(rows[1].net_income).toBe(-4_368e6)
  })

  it('merges series by period, oldest first, filling a missing period with null', () => {
    const data = fixtureFinancials({
      series: {
        fcf: { points: [{ period_end: '2025-01-26', label: 'FY2025', value: 2, yoy: 0.5 }], cagr: null, yoy: null, cagrs: { y3: null, y5: null, y10: null } },
        fcf_after_sbc: {
          points: [
            { period_end: '2025-01-26', label: 'FY2025', value: 1, yoy: null },
            { period_end: '2024-01-28', label: 'FY2024', value: 3, yoy: null },
          ],
          cagr: null, yoy: null, cagrs: { y3: null, y5: null, y10: null },
        },
      },
    })
    expect(chartRows(data, ['fcf', 'fcf_after_sbc'])).toEqual([
      { period_end: '2024-01-28', label: 'FY2024', fcf: null, fcf_after_sbc: 3, yoy: { fcf: null, fcf_after_sbc: null } },
      { period_end: '2025-01-26', label: 'FY2025', fcf: 2, fcf_after_sbc: 1, yoy: { fcf: 0.5, fcf_after_sbc: null } },
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
    expect(latestValue({ points: [], cagr: null, yoy: null, cagrs: { y3: null, y5: null, y10: null } })).toBeNull()
  })
})

describe('visibleCharts', () => {
  it('draws the dividend card only for a company with a dividend on record', () => {
    expect(visibleCharts(fixtureFinancials()).map((c) => c.id)).toContain('dps')
    const nonPayer = fixtureFinancials({ series: { dividends_per_share: fixtureSeries([null, null, null, null]) } })
    expect(visibleCharts(nonPayer).map((c) => c.id)).not.toContain('dps')
    expect(visibleCharts(nonPayer)).toHaveLength(COMPANY_CHARTS.length - 1)
    expect(visibleCharts(nonPayer, 'perShare')).toHaveLength(COMPANY_CHARTS.length - 1)
  })
})

describe('seriesWithData', () => {
  const margins = COMPANY_CHARTS.find((c) => c.id === 'margins')!
  it('leaves out a series the company never files', () => {
    const noGross = fixtureFinancials({ series: { gross_margin: fixtureSeries([null, null, null, null]) } })
    expect(seriesWithData(noGross, margins.total).map((s) => s.key)).toEqual(['operating_margin', 'net_margin', 'fcf_margin'])
    expect(seriesWithData(fixtureFinancials(), margins.total)).toHaveLength(4)
  })

  it('keeps the first series when none has a value, so the card still has a lead', () => {
    const empty = fixtureFinancials({ series: emptySeriesSet() })
    expect(seriesWithData(empty, margins.total).map((s) => s.key)).toEqual(['gross_margin'])
  })
})

describe('visibleKeyStats', () => {
  it('lists only the figures the company has', () => {
    const keys = visibleKeyStats(FIXTURE_KEY_STATS).map((d) => d.key)
    expect(keys).toContain('revenue')
    expect(keys).toContain('dividends_paid')
    expect(keys).toContain('pe_ratio')
    expect(visibleKeyStats({ ...FIXTURE_KEY_STATS, pe_ratio: null }).map((d) => d.key)).not.toContain('pe_ratio')
    expect(keys).not.toContain('gross_margin')
    expect(keys).not.toContain('buybacks')
    expect(visibleKeyStats(undefined)).toEqual([])
  })
})

describe('chartView', () => {
  const byId = (id: string) => COMPANY_CHARTS.find((c) => c.id === id)!
  const keys = (id: string, basis: 'total' | 'perShare') => chartView(byId(id), basis).series.map((s) => s.key)

  it('swaps each figure for its per-share series and keeps the rest', () => {
    expect(keys('revenue', 'perShare')).toEqual(['revenue_per_share'])
    expect(keys('net-income', 'perShare')).toEqual(['eps_diluted'])
    expect(keys('fcf', 'perShare')).toEqual(['fcf_per_share', 'fcf_after_sbc_per_share'])
    expect(keys('fcf', 'total')).toEqual(['fcf', 'fcf_after_sbc'])
    for (const id of ['shares', 'margins', 'dps']) expect(keys(id, 'perShare')).toEqual(keys(id, 'total'))
  })
})

describe('hasAnyData', () => {
  it('is false only when no series has a value', () => {
    expect(hasAnyData(fixtureFinancials())).toBe(true)
    expect(hasAnyData(fixtureFinancials({ series: emptySeriesSet() }))).toBe(false)
  })
})

describe('resolveCompany', () => {
  it('prefers the pick, then the remembered pick, then the first held company', () => {
    expect(resolveCompany(FIXTURE_COMPANIES, AMZN_CIK, NVDA_CIK)?.symbol).toBe('AMZN')
    expect(resolveCompany(FIXTURE_COMPANIES, null, AAPL_CIK)?.symbol).toBe('AAPL')
    expect(resolveCompany(FIXTURE_COMPANIES, null, null)?.symbol).toBe('NVDA')
    expect(resolveCompany(FIXTURE_COMPANIES.filter((c) => !c.held), null, null)?.symbol).toBe('AAPL')
  })

  it('drops a remembered company no longer in the list', () => {
    expect(resolveCompany(FIXTURE_COMPANIES, null, 999)?.symbol).toBe('NVDA')
    expect(resolveCompany([], null, NVDA_CIK)).toBeNull()
  })
})

describe('companyGroups', () => {
  it('puts the held companies first and the Nasdaq-100 after, leaving out an empty group', () => {
    expect(companyGroups(FIXTURE_COMPANIES).map((g) => [g.label, g.companies.map((c) => c.symbol)])).toEqual([
      ['Held', ['NVDA', 'AMZN']],
      ['Nasdaq-100', ['AAPL']],
    ])
    expect(companyGroups(FIXTURE_COMPANIES.filter((c) => c.held)).map((g) => g.label)).toEqual(['Held'])
  })
})
