import { describe, expect, it } from 'vitest'

/**
 * Review items M5 and DR5 (2026-11-07): four "% of the book" denominators had grown on the
 * client — Positions' weights and the Largest holding tile with cash, Top 5 and the
 * rebalance plan without — so one holding read two shares one card apart. `bookTotal`
 * (`positionValuation.ts`) is the one now.
 *
 * The question this asks is "is there a copy at all": a sum of market values is how every
 * one of those denominators began. Each that remains is named here with why it is not a
 * share of the book; a new one fails until it uses `bookTotal` or says why it cannot.
 */
const ALLOWED: Record<string, { count: number; why: string }> = {
  'lib/positionValuation.ts': { count: 1, why: 'bookTotal itself' },
  'lib/currencyExposure.ts': {
    count: 1,
    why: "holdings only on purpose: the client does not know the cash's currencies",
  },
  'lib/portfolioKpis.ts': {
    count: 1,
    why: 'herfindahlConcentration, an index over the holdings, not a share of the book',
  },
}

describe('one "% of the book" denominator', () => {
  it('no module sums market values for a total but the ones named here', () => {
    const sources = import.meta.glob(['../**/*.ts', '../**/*.tsx', '!../**/*.test.ts', '!../**/*.test.tsx'], {
      query: '?raw', import: 'default', eager: true,
    }) as Record<string, string>
    const found: Record<string, number> = {}
    for (const [path, source] of Object.entries(sources)) {
      // A running sum of market values (`s + p.market_value_eur`), not a max or a filter.
      const sums = source
        .split('\n')
        .filter((line) => /\.reduce\(.*\+\s*(Math\.max\(0,\s*)?\w+\.market_value_eur/.test(line)).length
      if (sums > 0) found[path.replace(/^\.\//, 'lib/').replace(/^\.\.\//, '')] = sums
    }
    expect(Object.keys(sources).length).toBeGreaterThan(50)
    expect(found).toEqual(Object.fromEntries(Object.entries(ALLOWED).map(([k, v]) => [k, v.count])))
  })
})
