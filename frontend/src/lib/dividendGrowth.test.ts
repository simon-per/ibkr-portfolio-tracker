import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import {
  feedRaises, growthPct, payoutChanges, payoutVerdict, RAISE_BAND_PCT, realizedOnlyYears,
} from './dividendGrowth'
import type { DividendAnnualRow, DividendRaise } from './api'

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

describe('growthPct', () => {
  it('copies the server rules: a zero or missing base has no growth', () => {
    expect(growthPct(10, 0)).toBeNull()
    expect(growthPct(10, null)).toBeNull()
    expect(growthPct(10, -5)).toBeNull()
  })

  it('rounds to the digits asked for, one by default', () => {
    expect(growthPct(372, 340)).toBe(9.4)
    expect(growthPct(372, 340, 2)).toBe(9.41)
    expect(growthPct(0, 50)).toBe(-100)
  })
})

describe('payoutVerdict', () => {
  it('is a raise or a cut only beyond the band, with the edge inside it', () => {
    expect(payoutVerdict(RAISE_BAND_PCT + 0.01)).toBe('raised')
    expect(payoutVerdict(RAISE_BAND_PCT)).toBe('flat')
    expect(payoutVerdict(-RAISE_BAND_PCT)).toBe('flat')
    expect(payoutVerdict(-RAISE_BAND_PCT - 0.01)).toBe('cut')
  })

  it('has no verdict without a figure', () => {
    expect(payoutVerdict(null)).toBeNull()
    expect(payoutVerdict(undefined)).toBeNull()
    expect(payoutVerdict(Number.NaN)).toBeNull()
  })
})

describe('feedRaises', () => {
  const move = (symbol: string, change_pct: number | null): DividendRaise => ({
    date: '2026-09-16', security_id: 1, symbol, currency: 'USD',
    level_from: 1, level_to: 1, change_pct, annual_change_eur: 1,
  })

  it("keeps the server's order and drops a move inside the band", () => {
    const out = feedRaises([move('AAA', 9.6), move('BBB', 0.4), move('CCC', -12), move('DDD', null)])
    expect(out.map((r) => r.symbol)).toEqual(['AAA', 'CCC'])
  })

  it('is empty without a history', () => {
    expect(feedRaises(undefined)).toEqual([])
  })
})

/**
 * The band is defined once (`RAISE_BAND_PCT`) and decided once (`payoutVerdict`), on this
 * side of the language boundary; the server's half of the pin is
 * `tests/test_dividend_raises_feed.py`, which refuses a band constant under `app/`. A copy
 * here would be a second answer to "is this a raise" beside the count.
 */
describe('one definition of a raise', () => {
  const SRC = join(__dirname, '..')
  const rel = (p: string) => relative(SRC, p).split(sep).join('/')
  const sources = (dir: string): string[] =>
    readdirSync(dir).flatMap((name) => {
      const path = join(dir, name)
      if (statSync(path).isDirectory()) return sources(path)
      return /\.(ts|tsx)$/.test(name) && !/\.test\.(ts|tsx)$/.test(name) ? [path] : []
    })
  const code = (path: string) =>
    readFileSync(path, 'utf8')
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .replace(/\/\/.*$/gm, '')

  it('declares the band in dividendGrowth.ts only', () => {
    const declaring = sources(SRC)
      .filter((p) => /\b(?:const|let|var)\s+\w*RAISE\w*BAND\w*\s*=/i.test(code(p)))
      .map(rel)
    expect(declaring).toEqual(['lib/dividendGrowth.ts'])
  })

  it('never compares a per-share change against a number of its own', () => {
    const comparing = sources(SRC)
      .filter((p) => rel(p) !== 'lib/dividendGrowth.ts')
      .filter((p) => /(?:per_share_growth_pct|per_share_change_pct|change_pct)\s*[<>]=?\s*-?(?:[1-9]|0\.\d|\(|[A-Z_])/.test(code(p)))
      .map(rel)
    expect(comparing).toEqual([])
  })
})

describe('payoutChanges', () => {
  const rows = (...pcts: (number | null | undefined)[]) =>
    pcts.map((per_share_growth_pct) => ({ per_share_growth_pct }))

  it('counts raises and cuts outside the band, and a move inside it as neither', () => {
    expect(payoutChanges(rows(8, 3.2, -12, 0.4, -0.9))).toEqual({
      raised: 2, cut: 1, flat: 2, measured: 5,
    })
  })

  it('puts the edge of the band inside it', () => {
    const out = payoutChanges(rows(RAISE_BAND_PCT, -RAISE_BAND_PCT, RAISE_BAND_PCT + 0.01))!
    expect(out.flat).toBe(2)
    expect(out.raised).toBe(1)
  })

  it('leaves a payer without a year of history out rather than calling it flat', () => {
    expect(payoutChanges(rows(5, null, undefined))).toEqual({
      raised: 1, cut: 0, flat: 0, measured: 1,
    })
  })

  it('is null when no payer is measured, never "0 raised · 0 cut"', () => {
    expect(payoutChanges(rows(null, undefined))).toBeNull()
    expect(payoutChanges([])).toBeNull()
  })
})
