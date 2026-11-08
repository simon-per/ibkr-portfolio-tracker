import { describe, expect, it } from 'vitest'

/**
 * Review items DR4 and U3. The server's PEG (`peg_ratio.resolve_peg`) falls back through
 * its tiers and refuses a negative P/E; two client copies re-derived it from
 * `trailing_pe / growth` and resurrected the negative one (-0.16, painted green). No module
 * under src/ divides a P/E for a PEG again.
 */
describe('one PEG, the served one', () => {
  it('no source divides trailing_pe by anything', () => {
    const sources = import.meta.glob(['../**/*.ts', '../**/*.tsx', '!../**/*.test.ts', '!../**/*.test.tsx'], {
      query: '?raw', import: 'default', eager: true,
    }) as Record<string, string>
    const offenders = Object.entries(sources)
      .filter(([, src]) => /trailing_pe\s*\//.test(src))
      .map(([path]) => path)
    expect(Object.keys(sources).length).toBeGreaterThan(50)
    expect(offenders).toEqual([])
  })
})
