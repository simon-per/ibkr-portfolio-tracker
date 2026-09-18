import { describe, expect, it } from 'vitest'
import { buildChartSeries, dividendTtmWindowLabel, duplicatedSymbols, FC, OTHER } from './dividendChart'
import type { DividendBreakdownResponse } from './api'

it('labels calendar TTM windows across year boundaries', () => {
  expect(dividendTtmWindowLabel('2026-01')).toBe('Feb 25 – Jan 26')
  expect(dividendTtmWindowLabel('2024-02')).toBe('Mar 23 – Feb 24')
  expect(dividendTtmWindowLabel('2025-12')).toBe('Jan 25 – Dec 25')
})

type Bucket = { month: string; actual?: Record<string, number>; forecast?: Record<string, number> }

const sum = (o: Record<string, number> = {}) => Object.values(o).reduce((a, b) => a + b, 0)

function response(months: Bucket[], ttm: (Bucket & { partial?: boolean })[] = []): DividendBreakdownResponse {
  return {
    years: [2026],
    year: 2026,
    months: months.map((m) => ({
      month: m.month,
      actual: m.actual ?? {},
      forecast: m.forecast ?? {},
      actual_total_eur: sum(m.actual),
      forecast_total_eur: sum(m.forecast),
    })),
    // Totals derived from the maps rather than stated, so a fixture cannot claim
    // a total its own segments contradict — the exact bug the backend test pins.
    ttm_series: ttm.map((p) => ({
      month: p.month,
      actual: p.actual ?? {},
      forecast: p.forecast ?? {},
      net_eur: sum(p.actual),
      forecast_net_eur: sum(p.forecast),
      total_eur: sum(p.actual) + sum(p.forecast),
      mom_pct: null,
      mom_includes_forecast: sum(p.forecast) > 0,
      source: 'ibkr',
      mom_crosses_era: false,
      partial: p.partial ?? false,
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
    expect(buildChartSeries(undefined)).toEqual({
      chartData: [], ttmData: [], ttmPoints: [], stackSymbols: [],
    })
  })
})

describe('the rolling series shares one ranking with the monthly one', () => {
  it('ranks a window by its size, not by how many windows a payment lands in', () => {
    // THE decisive case. A rolling window is a twelve-month total, so summing the
    // windows counts a January payment once per window it falls in (twelve) and a
    // December one exactly once. Rank by that sum and JAN scores 1300 against
    // DEC's 300 — symbols ordered by WHEN they paid, not how much, which would
    // quietly reorder the monthly stack too. The widest single window is a year of
    // income counted once.
    const months: Bucket[] = [{ month: '2026-01', actual: { JAN: 100 } },
                              { month: '2026-12', actual: { DEC: 150 } }]
    const ttm: Bucket[] = Array.from({ length: 12 }, (_, i) => {
      const actual: Record<string, number> = { JAN: 100 }
      if (i === 11) actual.DEC = 150
      return { month: `2026-${String(i + 1).padStart(2, '0')}`, actual }
    })
    // Eight fillers that outrank both, so nine slots leave exactly one of
    // JAN/DEC standing and the ranking rule alone decides which.
    for (const p of ttm) for (let f = 0; f < 8; f++) p.actual![`F${f}`] = 400
    const { stackSymbols } = buildChartSeries(response(months, ttm), { maxSeries: 9 })

    // Widest-window: DEC 300 beats JAN 200, on income.
    // Summed windows: JAN 1300 beats DEC 300, purely on paying early in the year.
    expect(stackSymbols).toContain('DEC')
    expect(stackSymbols).not.toContain('JAN')
  })

  it('gives both projections the same key space, so a symbol cannot change colour', () => {
    const { chartData, ttmData, stackSymbols } = buildChartSeries(response(
      [{ month: '2026-01', actual: { AAA: 5 }, forecast: { BBB: 2 } }],
      [{ month: '2026-01', actual: { AAA: 60 }, forecast: { BBB: 9 } }],
    ))
    const keys = [...chartData, ...ttmData]
      .flatMap((r) => Object.keys(r))
      .filter((k) => k !== 'month')
      .map((k) => (k.startsWith(FC) ? k.slice(FC.length) : k))
    expect(keys.length).toBeGreaterThan(0)
    for (const k of keys) expect(stackSymbols).toContain(k)
  })

  it('splits the rolling rows into measured and projected on the same FC prefix', () => {
    const { ttmData } = buildChartSeries(response(
      [{ month: '2026-08', actual: { AAA: 5 } }],
      [{ month: '2026-08', actual: { AAA: 50 }, forecast: { AAA: 12 } }],
    ))
    expect(ttmData[0]['AAA']).toBe(50)
    expect(ttmData[0][FC + 'AAA']).toBe(12)
  })

  it('folds beyond-palette symbols into Other on the rolling rows too', () => {
    const actual: Record<string, number> = {}
    for (let i = 0; i < 11; i++) actual[`S${i}`] = i + 1
    const { ttmData, stackSymbols } = buildChartSeries(response(
      [{ month: '2026-02', actual }], [{ month: '2026-02', actual }],
    ))
    expect(stackSymbols[stackSymbols.length - 1]).toBe(OTHER)
    expect(ttmData[0][OTHER]).toBe(6)      // 1 + 2 + 3, same rule as the bars
  })

  it('hides open windows when the forecast is off, without repainting anything', () => {
    const data = response(
      [{ month: '2026-08', actual: { AAA: 5 } }],
      [{ month: '2026-08', actual: { AAA: 50 } },
       { month: '2026-09', actual: { AAA: 40 }, forecast: { ZZZ: 9 }, partial: true }],
    )
    const on = buildChartSeries(data, { showForecast: true })
    const off = buildChartSeries(data, { showForecast: false })

    expect(on.ttmPoints.map((p) => p.month)).toEqual(['2026-08', '2026-09'])
    expect(off.ttmPoints.map((p) => p.month)).toEqual(['2026-08'])
    // Ranking reads the unfiltered series, so toggling cannot move a colour — the
    // projection-only symbol keeps its slot either way.
    expect(off.stackSymbols).toEqual(on.stackSymbols)
    expect(on.stackSymbols).toContain('ZZZ')
  })

  it('keeps rows and points aligned, which is what the chart relies on', () => {
    const data = response(
      [{ month: '2026-08', actual: { AAA: 5 } }],
      [{ month: '2026-07', actual: { AAA: 30 } },
       { month: '2026-08', actual: { AAA: 50 } },
       { month: '2026-09', actual: { AAA: 40 }, partial: true }],
    )
    for (const showForecast of [true, false]) {
      const { ttmData, ttmPoints } = buildChartSeries(data, { showForecast })
      expect(ttmData).toHaveLength(ttmPoints.length)
      ttmData.forEach((row, i) => expect(row.month).toBe(ttmPoints[i].month))
    }
  })

  it('trims nothing itself — the server sends only covered windows', () => {
    // months[] reaches back further than the rolling series can cover, and the
    // client must not invent points for the gap or strip any it was given.
    const { ttmData, chartData } = buildChartSeries(response(
      [{ month: '2025-01', actual: { AAA: 1 } }, { month: '2026-01', actual: { AAA: 2 } }],
      [{ month: '2026-01', actual: { AAA: 3 } }],
    ))
    expect(chartData).toHaveLength(2)
    expect(ttmData).toHaveLength(1)
    expect(ttmData[0].month).toBe('2026-01')
  })

  it('survives a response with no rolling series at all', () => {
    const { ttmData, ttmPoints, stackSymbols } = buildChartSeries(
      response([{ month: '2026-01', actual: { AAA: 5 } }]),
    )
    expect(ttmData).toEqual([])
    expect(ttmPoints).toEqual([])
    expect(stackSymbols).toEqual(['AAA'])
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
