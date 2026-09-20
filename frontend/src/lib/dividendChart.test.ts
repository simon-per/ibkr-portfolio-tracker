import { describe, expect, it } from 'vitest'
import {
  buildChartSeries, dividendTtmWindowLabel, duplicatedSymbols, FC, legendEntries, OTHER,
} from './dividendChart'
import { dividendColor, SERIES_IDENTITIES } from './dividendColors'
import type { DividendBreakdownResponse } from './api'

it('labels calendar TTM windows across year boundaries', () => {
  expect(dividendTtmWindowLabel('2026-01')).toBe('Feb 25 – Jan 26')
  expect(dividendTtmWindowLabel('2024-02')).toBe('Mar 23 – Feb 24')
  expect(dividendTtmWindowLabel('2025-12')).toBe('Jan 25 – Dec 25')
})

type Bucket = { month: string; actual?: Record<string, number>; forecast?: Record<string, number> }

const sum = (o: Record<string, number> = {}) => Object.values(o).reduce((a, b) => a + b, 0)

type TtmBucket = Bucket & { partial?: boolean; mom_pct?: number | null }

/**
 * The server's own ordering rule, so a fixture cannot quietly disagree with it: every
 * symbol with income or a projection, biggest first, symbol as the tie-break. It sums the
 * rolling buckets here as well as the monthly ones purely because a fixture's `months` is a
 * short slice — on the wire the monthly maps are unwindowed and already contain everything
 * the rolling series can.
 */
function orderOf(months: Bucket[], ttm: TtmBucket[]): string[] {
  const totals = new Map<string, number>()
  for (const b of [...months, ...ttm]) {
    for (const [s, v] of [...Object.entries(b.actual ?? {}), ...Object.entries(b.forecast ?? {})]) {
      totals.set(s, (totals.get(s) ?? 0) + v)
    }
  }
  return [...totals.entries()]
    .sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1))
    .map(([s]) => s)
}

function response(
  months: Bucket[], ttm: TtmBucket[] = [], stackOrder?: string[],
): DividendBreakdownResponse {
  return {
    years: [2026],
    year: 2026,
    stack_order: stackOrder ?? orderOf(months, ttm),
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
      mom_pct: p.mom_pct ?? null,
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
    forecast_withholding_pct: 15,
  }
}

describe('buildChartSeries', () => {
  it('keys the rows on the buckets\' own symbols, not on bare tickers', () => {
    // The regression: the backend disambiguates a dual-listed ticker, so anything
    // matching on securities[].symbol ("ASML") matched neither key and dropped both
    // into Other. `stack_order` carries the same disambiguated labels the buckets do.
    const data = response([
      { month: '2026-02', actual: { 'ASML (NASDAQ)': 10, 'ASML (AEB)': 3 } },
    ])
    const { stackSymbols, chartData } = buildChartSeries(data)

    expect(stackSymbols).toEqual(['ASML (NASDAQ)', 'ASML (AEB)'])
    expect(stackSymbols).not.toContain(OTHER)
    expect(chartData[0]['ASML (NASDAQ)']).toBe(10)
  })

  it('draws the first SERIES_IDENTITIES of stack_order and folds the rest', () => {
    const actual: Record<string, number> = {}
    for (let i = 0; i < SERIES_IDENTITIES + 3; i++) actual[`S${i}`] = i + 1   // last is largest
    const { stackSymbols, chartData } = buildChartSeries(response([
      { month: '2026-02', actual },
    ]))

    expect(stackSymbols).toHaveLength(SERIES_IDENTITIES + 1)   // identities + Other
    expect(stackSymbols[stackSymbols.length - 1]).toBe(OTHER)
    expect(stackSymbols).toContain(`S${SERIES_IDENTITIES + 2}`)   // biggest kept
    expect(stackSymbols).not.toContain('S0')                      // smallest folded
    expect(chartData[0][OTHER]).toBe(6)                           // 1 + 2 + 3
  })

  it('takes the order it is given, whatever this range happens to be worth', () => {
    // The whole point: the order is the SERVER's, measured over the whole history, so a
    // range in which the biggest payer happens to pay nothing does not promote anyone.
    const { stackSymbols } = buildChartSeries(response(
      [{ month: '2026-02', actual: { SMALL: 1, BIG: 99 } }], [], ['SMALL', 'BIG'],
    ))
    expect(stackSymbols).toEqual(['SMALL', 'BIG'])
  })

  it('carries forecast on its own prefixed key so one stack holds both', () => {
    const { chartData } = buildChartSeries(response([
      { month: '2026-08', actual: { AAA: 5 }, forecast: { AAA: 7 } },
    ]))
    expect(chartData[0]['AAA']).toBe(5)
    expect(chartData[0][FC + 'AAA']).toBe(7)
  })

  it('draws a projection-only symbol as itself, so toggling cannot repaint series', () => {
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

  it('keeps elapsed gaps but removes future month labels when forecast is off', () => {
    const data = response([
      { month: '2026-08' },
      { month: '2026-09', actual: { AAA: 5 } },
      { month: '2026-10', forecast: { AAA: 6 } },
      { month: '2026-11' },
      { month: '2026-12', forecast: { AAA: 6 } },
    ])

    const on = buildChartSeries(data, { showForecast: true, currentMonth: '2026-09' })
    const off = buildChartSeries(data, { showForecast: false, currentMonth: '2026-09' })

    expect(on.chartData.map((row) => row.month)).toEqual([
      '2026-08', '2026-09', '2026-10', '2026-11', '2026-12',
    ])
    expect(off.chartData.map((row) => row.month)).toEqual(['2026-08', '2026-09'])
  })

  it('handles no data at all', () => {
    expect(buildChartSeries(undefined)).toEqual({
      chartData: [], ttmData: [], ttmPoints: [], stackSymbols: [],
    })
  })
})

/**
 * The family question behind the whole change: can ANY view repaint a holding?
 *
 * Not "do these two views agree" — agreement is what a per-view ranking looked like right
 * up to the moment it stopped. Colour is asked for across ranges that share almost nothing,
 * with the forecast on and off and both chart modes, and the answer has to be one colour per
 * symbol every time. This is the test the old `palette[stackSymbols.indexOf(sym)]` failed:
 * on production, All time and 2025 shared four of eight symbols and GOOGL moved three slots.
 */
describe('a holding cannot change colour when the view changes', () => {
  const ORDER = ['VT', 'ASML (NASDAQ)', 'QQQM', 'SPGI', 'META', 'GOOGL', '2330', 'GOOG',
                 'MA', 'ASML (AEB)', 'SOXQ', 'AVGO', 'GRID', 'NVDA', 'CRM']

  const ranges: Record<string, DividendBreakdownResponse> = {
    all: response(
      [{ month: '2024-06', actual: { GOOG: 2, AVGO: 3, NVDA: 1 } },
       { month: '2025-03', actual: { 'ASML (AEB)': 5, CRM: 1, META: 4 } },
       { month: '2026-02', actual: { VT: 40, QQQM: 19, SPGI: 16 }, forecast: { MA: 6 } }],
      [{ month: '2026-02', actual: { VT: 40, QQQM: 19 }, partial: true }],
      ORDER,
    ),
    '2025': response(
      [{ month: '2025-03', actual: { 'ASML (AEB)': 5, CRM: 1, META: 4 } }], [], ORDER,
    ),
    '2024': response(
      [{ month: '2024-06', actual: { GOOG: 2, AVGO: 3, NVDA: 1 } }], [], ORDER,
    ),
  }

  it('gives a symbol one colour across every range, mode and toggle state', () => {
    const seen = new Map<string, string>()
    for (const data of Object.values(ranges)) {
      for (const showForecast of [true, false]) {
        const { stackSymbols } = buildChartSeries(data, { showForecast })
        for (const symbol of stackSymbols) {
          const color = dividendColor(symbol, data.stack_order)
          const prior = seen.get(symbol)
          if (prior !== undefined) expect(color).toBe(prior)
          seen.set(symbol, color)
        }
      }
    }
    // The ranges really are different books, or the assertion above is vacuous.
    expect(seen.size).toBeGreaterThan(8)
    expect(buildChartSeries(ranges['2024']).stackSymbols)
      .not.toEqual(buildChartSeries(ranges['2025']).stackSymbols)
  })

  it('keeps a symbol in the same place in the stack, not just the same colour', () => {
    // Stack order is `stack_order` order, so a holding does not jump up and down the bar
    // between ranges either — which is the same instability with a different symptom.
    const a = buildChartSeries(ranges.all).stackSymbols.filter((s) => s !== OTHER)
    const b = buildChartSeries(ranges['2025']).stackSymbols.filter((s) => s !== OTHER)
    const relative = a.filter((s) => b.includes(s))
    expect(b.filter((s) => a.includes(s))).toEqual(relative)
  })

  it('colours the fold, and anything the order does not carry, as Other', () => {
    const other = dividendColor(OTHER, ORDER)
    expect(dividendColor('NEVER-HEARD-OF-IT', ORDER)).toBe(other)
    expect(dividendColor(ORDER[SERIES_IDENTITIES], ORDER)).toBe(other)
    expect(dividendColor(ORDER[0], ORDER)).not.toBe(other)
  })
})

describe('legendEntries', () => {
  it('orders by what this range paid and sums the projected half in', () => {
    const rows = [
      { month: '2026-01', AAA: 1, [FC + 'BBB']: 9 },
      { month: '2026-02', AAA: 2, BBB: 1 },
    ]
    expect(legendEntries(rows, ['AAA', 'BBB'])).toEqual([
      { symbol: 'BBB', total: 10 },
      { symbol: 'AAA', total: 3 },
    ])
  })

  it('leaves out a symbol with no segment on screen', () => {
    const rows = [{ month: '2026-01', AAA: 1 }]
    expect(legendEntries(rows, ['AAA', 'GONE']).map((e) => e.symbol)).toEqual(['AAA'])
  })
})

describe('the rolling series shares one ordering with the monthly one', () => {
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
    for (let i = 0; i < SERIES_IDENTITIES + 3; i++) actual[`S${i}`] = i + 1
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
    expect(on.stackSymbols).toContain('ZZZ')
    // Hiding the projections leaves the projection-only symbol nothing to draw, so it stops
    // being offered a swatch for a segment that is not there. Nothing repaints either way:
    // a colour is the symbol's rank in `stack_order`, which no toggle reaches. The old
    // version had to rank over the UNFILTERED series to get the same guarantee.
    expect(off.stackSymbols).toEqual(on.stackSymbols.filter((s) => s !== 'ZZZ'))
  })

  it('strips an unsettled payment from a closed window instead of dropping it', () => {
    // A dividend that has gone ex and not paid puts projection inside a window
    // that HAS fully elapsed. Dropping that point would make twelve months of
    // measured income vanish over one late payment, so it stays and loses its
    // forecast half — and everything read off it has to describe what is drawn.
    const data = response(
      [{ month: '2026-08', actual: { AAA: 5 }, forecast: { AAA: 2 } }],
      [{ month: '2026-07', actual: { AAA: 100 }, mom_pct: 10 },
       { month: '2026-08', actual: { AAA: 120 }, forecast: { AAA: 2 }, mom_pct: 22 },
       { month: '2026-09', actual: { AAA: 130 }, forecast: { AAA: 9 }, partial: true }],
    )
    const { ttmPoints, ttmData } = buildChartSeries(data, { showForecast: false })

    expect(ttmPoints.map((p) => p.month)).toEqual(['2026-07', '2026-08'])
    const closed = ttmPoints[1]
    expect(closed.forecast).toEqual({})
    expect(closed.forecast_net_eur).toBe(0)
    expect(closed.total_eur).toBe(120)         // not 122 — the bar's real height
    expect(closed.mom_includes_forecast).toBe(false)
    expect(closed.mom_pct).toBe(20)            // 120 over 100, not the server's 22
    // No forecast key survives into the row either, or the legend would offer a
    // "translucent = forecast" swatch for a segment that is not drawn.
    expect(ttmData[1][FC + 'AAA']).toBeUndefined()
    // Untouched where there is nothing to strip: the same object, not a copy.
    expect(ttmPoints[0]).toBe(data.ttm_series![0])
  })

  it('reports no change rather than a wrong one for the first window shown', () => {
    // A windowed response is already sliced, so the earliest point's predecessor
    // is a month the client never received. Measuring against the point after it
    // would make a window's change depend on the range showing it — the thing the
    // server builds the series whole to prevent — so it reports nothing.
    const data = response(
      [{ month: '2026-08', actual: { AAA: 5 } }],
      [{ month: '2026-08', actual: { AAA: 120 }, forecast: { AAA: 2 }, mom_pct: 22 }],
    )
    const { ttmPoints } = buildChartSeries(data, { showForecast: false })
    expect(ttmPoints[0].mom_pct).toBeNull()
    expect(ttmPoints[0].total_eur).toBe(120)
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
