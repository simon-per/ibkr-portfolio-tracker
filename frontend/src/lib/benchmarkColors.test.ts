import { describe, expect, it } from 'vitest'
import {
  BENCHMARK_COLORS, PORTFOLIO_SERIES_COLORS, UNLISTED_BENCHMARK_COLOR, benchmarkColor,
} from './benchmarkColors'

/**
 * Colour by identity. Both the chart and the picker used to index the palette by the
 * benchmark's position in the SELECTION, so deselecting the first of three recoloured the
 * other two — the reader who had learned "S&P is blue" watched blue become the Nasdaq.
 */

const all = [{ key: 'sp500' }, { key: 'nasdaq' }, { key: 'msci_world' }, { key: 'smi' }]

describe('benchmarkColor', () => {
  it('colours by position in the full list', () => {
    expect(benchmarkColor('sp500', all)).toBe(BENCHMARK_COLORS[0])
    expect(benchmarkColor('msci_world', all)).toBe(BENCHMARK_COLORS[2])
  })

  it('keeps a colour when a neighbour is deselected', () => {
    // The regression: `selected.indexOf` gave the second selection slot 1 until the first
    // was removed, then slot 0.
    const before = ['sp500', 'nasdaq'].map((k) => benchmarkColor(k, all))
    const after = ['nasdaq'].map((k) => benchmarkColor(k, all))
    expect(after[0]).toBe(before[1])
    expect(after[0]).not.toBe(before[0])
  })

  it('is independent of the order things were selected in', () => {
    const a = ['smi', 'sp500'].map((k) => benchmarkColor(k, all))
    const b = ['sp500', 'smi'].map((k) => benchmarkColor(k, all))
    expect(a).toEqual([b[1], b[0]])
  })

  it('is grey for a key the list does not carry, or before the list has loaded', () => {
    // A stored selection can name a benchmark the backend no longer offers. Grey says
    // "unidentified"; slot 0's blue would impersonate the first real one.
    expect(benchmarkColor('gone', all)).toBe(UNLISTED_BENCHMARK_COLOR)
    expect(benchmarkColor('sp500', undefined)).toBe(UNLISTED_BENCHMARK_COLOR)
    expect(BENCHMARK_COLORS).not.toContain(UNLISTED_BENCHMARK_COLOR)
  })

  it('wraps around a list longer than the palette', () => {
    const many = Array.from({ length: BENCHMARK_COLORS.length + 1 }, (_, i) => ({ key: `b${i}` }))
    expect(benchmarkColor(`b${BENCHMARK_COLORS.length}`, many)).toBe(BENCHMARK_COLORS[0])
  })
})

/** OKLab distance ×100 under normal vision — the dataviz validator's "normal-vision floor". */
function deltaE(a: string, b: string): number {
  const lab = (hex: string) => {
    const [r, g, bl] = [1, 3, 5].map((i) => {
      const c = parseInt(hex.slice(i, i + 2), 16) / 255
      return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
    })
    const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * bl)
    const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * bl)
    const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * bl)
    return [
      0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
      1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
      0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
    ]
  }
  const [x, y] = [lab(a), lab(b)]
  return 100 * Math.hypot(x[0] - y[0], x[1] - y[1], x[2] - y[2])
}

describe('benchmark hues against the value chart\'s own lines', () => {
  const series = Object.values(PORTFOLIO_SERIES_COLORS)

  it('never share a hue with the portfolio, its cash, its profit or its money in', () => {
    // Slot 0 was the portfolio line's own blue, so the S&P 500 drew as the portfolio.
    for (const b of BENCHMARK_COLORS) {
      for (const s of series) {
        expect(deltaE(b, s), `${b} vs ${s}`).toBeGreaterThanOrEqual(15)
      }
    }
  })

  it('keeps the first three — the likeliest trio under the cap — apart from each other', () => {
    const [a, b, c] = BENCHMARK_COLORS
    for (const [x, y] of [[a, b], [a, c], [b, c]]) {
      expect(deltaE(x, y), `${x} vs ${y}`).toBeGreaterThanOrEqual(15)
    }
  })

  it('keeps the portfolio\'s own lines apart from each other', () => {
    for (let i = 0; i < series.length; i++) {
      for (let j = i + 1; j < series.length; j++) {
        expect(deltaE(series[i], series[j]), `${series[i]} vs ${series[j]}`).toBeGreaterThanOrEqual(15)
      }
    }
  })
})
