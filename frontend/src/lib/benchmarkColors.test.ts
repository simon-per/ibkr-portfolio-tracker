import { describe, expect, it } from 'vitest'
import { BENCHMARK_COLORS, UNLISTED_BENCHMARK_COLOR, benchmarkColor } from './benchmarkColors'

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
