import { describe, expect, it } from 'vitest'

/**
 * Every value axis measures its own labels (`width="auto"`).
 *
 * A fixed width is sized for one currency: "€12k" fits 44px, "CHF 120k" does not, and the
 * base currency is switchable, so the leading "C" of "CHF" was clipped on the Performance
 * and Forecast charts (browser sweep, 2026-10-14). Category axes of symbol names keep a
 * fixed width on purpose — measured, one long name would take the plot.
 */
describe('chart value axes', () => {
  it('size themselves from their tick labels', () => {
    const sources = import.meta.glob('./*.tsx', { query: '?raw', import: 'default', eager: true })
    const offenders: string[] = []
    let valueAxes = 0
    for (const [path, src] of Object.entries(sources)) {
      if (path.endsWith('.test.tsx')) continue
      // Up to the self-closing `/>`, not the first `>`: tick formatters are arrow functions.
      for (const match of (src as string).matchAll(/<YAxis\b[\s\S]*?\/>/g)) {
        const axis = match[0]
        if (/type="category"/.test(axis)) continue
        valueAxes += 1
        if (!/width="auto"/.test(axis)) offenders.push(`${path}: ${axis.replace(/\s+/g, ' ')}`)
      }
    }
    expect(offenders).toEqual([])
    // A floor, so a parser that stops matching cannot pass on zero axes.
    expect(valueAxes).toBeGreaterThanOrEqual(10)
  })
})
