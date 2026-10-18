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

/**
 * Every compact money label goes through `formatAxisMoney`.
 *
 * Seven charts each divided by a thousand inline, and each copy was wrong somewhere —
 * "CHF 1000k" for a million on the Forecast axis, "CHF 0k" for zero, a 12,500 tick
 * rounded to "13k" on the value chart. A new chart reaching for `/ 1000` gets one of
 * those back.
 */
describe('compact money labels', () => {
  it('are never hand-rolled with a division by a thousand', () => {
    const sources = import.meta.glob('./**/*.tsx', { query: '?raw', import: 'default', eager: true })
    const offenders: string[] = []
    for (const [path, src] of Object.entries(sources)) {
      if (path.endsWith('.test.tsx')) continue
      for (const match of (src as string).matchAll(/\/\s*1000\s*\)\s*\.toFixed\(/g)) {
        offenders.push(`${path}: …${(src as string).slice(Math.max(0, match.index! - 40), match.index! + 20)}…`)
      }
    }
    expect(offenders).toEqual([])
  })

  // The dividend forecast axis printed `${curSym}${Math.round(value)}`: "CHF 1234", no
  // separator and no compact unit.
  it('reach the currency symbol only through formatAxisMoney', () => {
    const sources = import.meta.glob('./**/*.tsx', { query: '?raw', import: 'default', eager: true })
    const offenders: string[] = []
    for (const [path, src] of Object.entries(sources)) {
      if (path.endsWith('.test.tsx')) continue
      for (const match of (src as string).matchAll(/<[XY]Axis\b[\s\S]*?\/>/g)) {
        if (/curSym/.test(match[0]) && !/formatAxisMoney\(/.test(match[0])) {
          offenders.push(`${path}: ${match[0].replace(/\s+/g, ' ')}`)
        }
      }
    }
    expect(offenders).toEqual([])
  })
})

/** Every chart draws one grid — see `CHART_GRID_PROPS`. */
describe('chart grids', () => {
  it('all spread the shared grid props', () => {
    const sources = import.meta.glob('./**/*.tsx', { query: '?raw', import: 'default', eager: true })
    const offenders: string[] = []
    let grids = 0
    for (const [path, src] of Object.entries(sources)) {
      if (path.endsWith('.test.tsx')) continue
      for (const match of (src as string).matchAll(/<CartesianGrid\b[\s\S]*?\/>/g)) {
        grids += 1
        if (!match[0].includes('{...CHART_GRID_PROPS}')) offenders.push(`${path}: ${match[0]}`)
      }
    }
    expect(offenders).toEqual([])
    expect(grids).toBeGreaterThanOrEqual(10)
  })
})
