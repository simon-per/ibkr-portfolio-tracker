import { describe, expect, it } from 'vitest'
import { buildChartSeries, dividendTtmWindowLabel, duplicatedSymbols, FC, OTHER } from './dividendChart'
import type { DividendBreakdownResponse } from './api'

it('labels calendar TTM windows across year boundaries', () => {
  expect(dividendTtmWindowLabel('2026-01')).toBe('Feb 25 – Jan 26')
  expect(dividendTtmWindowLabel('2024-02')).toBe('Mar 23 – Feb 24')
  expect(dividendTtmWindowLabel('2025-12')).toBe('Jan 25 – Dec 25')
})

function response(
  months: { month: string; actual?: Record<string, number>; forecast?: Record<string, number> }[],
): DividendBreakdownResponse {
  return {
    years: [2026],
    year: 2026,
    months: months.map((m) => ({
      month: m.month,
      actual: m.actual ?? {},
      forecast: m.forecast ?? {},
      actual_total_eur: Object.values(m.actual ?? {}).reduce((a, b) => a + b, 0),
      forecast_total_eur: Object.values(m.forecast ?? {}).reduce((a, b) => a + b, 0),
    })),
    securities: [],
    total_net_eur: 0,
    total_forecast_net_eur: 0,
    ibkr_from: null,
    base_currency: 'CHF',
  }
}

describe('buildChartSeries', () => {
  it('ranks series from the month buckets, not from bare symbols', () => {
    // The regression: the backend disambiguates a dual-listed ticker, so ranking
    // by securities[].symbol ("ASML") matched neither key and dropped both into
    // Other. Ranking from the buckets is what keeps them as real series.
    const data = response([
      { month: '2026-02', actual: { 'ASML (NASDAQ)': 10, 'ASML (AEB)': 3 } },
    ])
    const { stackSymbols, chartData } = buildChartSeries(data)

    expect(stackSymbols).toEqual(['ASML (AEB)', 'ASML (NASDAQ)'])
    expect(stackSymbols).not.toContain(OTHER)
    expect(chartData[0]['ASML (NASDAQ)']).toBe(10)
  })

  it('keeps the biggest series and folds the rest into one bucket', () => {
    const actual: Record<string, number> = {}
    for (let i = 0; i < 11; i++) actual[`S${i}`] = i + 1   // S10 largest
    const { stackSymbols, chartData } = buildChartSeries(response([
      { month: '2026-02', actual },
    ]))

    expect(stackSymbols).toHaveLength(9)           // 8 series + Other
    expect(stackSymbols[stackSymbols.length - 1]).toBe(OTHER)
    expect(stackSymbols).toContain('S10')          // biggest kept
    expect(stackSymbols).not.toContain('S0')       // smallest folded
    expect(chartData[0][OTHER]).toBe(6)            // 1 + 2 + 3
  })

  it('carries forecast on its own prefixed key so one stack holds both', () => {
    const { chartData } = buildChartSeries(response([
      { month: '2026-08', actual: { AAA: 5 }, forecast: { AAA: 7 } },
    ]))
    expect(chartData[0]['AAA']).toBe(5)
    expect(chartData[0][FC + 'AAA']).toBe(7)
  })

  it('ranks on actual and forecast together, so toggling cannot repaint series', () => {
    // A series that has only forecast still earns its slot and its colour.
    const { stackSymbols } = buildChartSeries(response([
      { month: '2026-08', actual: { AAA: 1 }, forecast: { ZZZ: 99 } },
    ]))
    expect(stackSymbols).toContain('ZZZ')
  })

  it('emits a row per month even when the month is empty', () => {
    const { chartData } = buildChartSeries(response([
      { month: '2026-01' }, { month: '2026-02', actual: { AAA: 1 } },
    ]))
    expect(chartData.map((r) => r.month)).toEqual(['2026-01', '2026-02'])
    expect(chartData[0]['AAA']).toBeUndefined()
  })

  it('handles no data at all', () => {
    expect(buildChartSeries(undefined)).toEqual({ chartData: [], stackSymbols: [] })
  })
})

describe('duplicatedSymbols', () => {
  it('flags only symbols listed more than once', () => {
    const dup = duplicatedSymbols([
      { symbol: 'ASML' }, { symbol: 'ASML' }, { symbol: 'SPGI' },
    ])
    expect([...dup]).toEqual(['ASML'])
  })
})
