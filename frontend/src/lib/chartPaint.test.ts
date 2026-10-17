/// <reference types="node" />
// Reads index.css off disk for the reason chartPalette.test.ts does: vitest replaces CSS
// imports with empty strings, so `?raw` would measure nothing and pass vacuously.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { CHART_OTHER, CHART_SLOTS } from './chartPalette'
import {
  DARK_LABEL_INK, LIGHT_LABEL_INK, NEUTRAL_INK, NEUTRAL_MIX, NEUTRAL_PAINT, chartPaint,
} from './chartPaint'
import { contrast, cssDeclarations, deltaE, hslTripleToHex } from './colorScience'

const CSS = readFileSync(fileURLToPath(new URL('../index.css', import.meta.url)), 'utf8')
type Theme = 'light' | 'dark'
const THEMES: Theme[] = ['light', 'dark']

const token = (theme: Theme, name: string) => cssDeclarations(CSS, name)[theme === 'light' ? 0 : 1]
const hslToken = (theme: Theme, name: string) => hslTripleToHex(token(theme, name))

/** `color-mix(in srgb, a p%, b)`: a straight mix of the gamma-encoded channels. */
function mixSrgb(a: string, pct: number, b: string): string {
  const ch = (hex: string, i: number) => parseInt(hex.slice(1 + 2 * i, 3 + 2 * i), 16)
  return `#${[0, 1, 2]
    .map((i) => Math.round(ch(a, i) * (pct / 100) + ch(b, i) * (1 - pct / 100)).toString(16).padStart(2, '0'))
    .join('')}`
}

function neutralHex(theme: Theme, role: keyof typeof NEUTRAL_PAINT): string {
  const other = token(theme, 'chart-other')
  if (role === 'tail') return other
  const { toward, pct } = NEUTRAL_MIX[role]
  return mixSrgb(other, pct, hslToken(theme, toward))
}

/** A tile or segment label is text on a fill; 3:1 is the large/bold-text floor it is set at. */
const LABEL_FLOOR = 3

describe('chartPaint hands out the palette with an ink per slot', () => {
  it('uses the palette fill and folds past the last slot to the Other neutral', () => {
    for (let i = 0; i < CHART_SLOTS; i++) expect(chartPaint(i).fill).toBe(`var(--chart-${i + 1})`)
    expect(chartPaint(CHART_SLOTS)).toBe(NEUTRAL_PAINT.tail)
    expect(chartPaint(-1)).toBe(NEUTRAL_PAINT.tail)
    expect(NEUTRAL_PAINT.tail.fill).toBe(CHART_OTHER)
  })

  it('is deterministic: the same slot always gets the same paint', () => {
    expect(chartPaint(3)).toEqual(chartPaint(3))
    expect(chartPaint(4).ink).toBe(DARK_LABEL_INK)
    expect(chartPaint(0).ink).toBe(LIGHT_LABEL_INK)
  })
})

describe.each(THEMES)('in the %s theme', (theme) => {
  it('every slot label clears 3:1 against its own fill', () => {
    for (let i = 0; i < CHART_SLOTS; i++) {
      const fill = token(theme, `chart-${i + 1}`)
      const ink = chartPaint(i).ink
      expect(contrast(ink, fill), `slot ${i + 1} ink ${ink} on ${fill}`).toBeGreaterThanOrEqual(LABEL_FLOOR)
    }
  })

  it('the neutral ink clears 3:1 on all three neutrals', () => {
    expect(NEUTRAL_INK).toBe('hsl(var(--foreground))')
    const ink = hslToken(theme, 'foreground')
    for (const role of ['tail', 'faint', 'strong'] as const) {
      expect(contrast(ink, neutralHex(theme, role)), role).toBeGreaterThanOrEqual(LABEL_FLOOR)
    }
  })

  it('keeps the three neutrals apart from each other and visible on the card', () => {
    const [tail, faint, strong] = (['tail', 'faint', 'strong'] as const).map((r) => neutralHex(theme, r))
    expect(deltaE(tail, faint)).toBeGreaterThanOrEqual(8)
    expect(deltaE(tail, strong)).toBeGreaterThanOrEqual(8)
    expect(deltaE(faint, strong)).toBeGreaterThanOrEqual(15)
    const card = hslToken(theme, 'card')
    for (const hex of [tail, faint, strong]) expect(contrast(hex, card), hex).toBeGreaterThanOrEqual(1.3)
  })
})
