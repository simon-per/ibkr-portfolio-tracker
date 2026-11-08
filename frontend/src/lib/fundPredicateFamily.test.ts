import { describe, expect, it } from 'vitest'

/**
 * Review items VI4 and R7. Fundamentals split its tables by a client `quote_type !== 'ETF'`
 * — a blacklist that read a MUTUALFUND and the two NAV-priced 3a funds (no Yahoo quote
 * type) as stock rows of dashes. The server's `is_fund` is the one predicate now; no
 * module under src/ compares a quote type again.
 */
describe('one fund predicate, the served one', () => {
  it('no source compares quote_type with a literal', () => {
    const sources = import.meta.glob(['../**/*.ts', '../**/*.tsx', '!../**/*.test.ts', '!../**/*.test.tsx'], {
      query: '?raw', import: 'default', eager: true,
    }) as Record<string, string>
    const offenders = Object.entries(sources)
      .filter(([, src]) => /quote_type\s*[!=]==?\s*['"]/.test(src))
      .map(([path]) => path)
    expect(Object.keys(sources).length).toBeGreaterThan(50)
    expect(offenders).toEqual([])
  })
})
