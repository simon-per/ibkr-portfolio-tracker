/// <reference types="node" />
// Reads index.css off disk, like dividendColors.test.ts and for its reason: vitest replaces
// CSS imports with empty strings, so `?raw` would measure an empty palette and pass vacuously.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
// @ts-expect-error — plain JS with no types; the config itself is what is being pinned.
import tailwindConfig from '../../tailwind.config.js'
import {
  CHART_OTHER, CHART_PALETTE, CHART_SLOTS, GAIN_COLOR, LOSS_COLOR, NEUTRAL_COLOR, chartColor,
  chartColorFor, signColor,
} from './chartPalette'
import { contrast, cssDeclarations, cvdDeltaE, deltaE, hslTripleToHex, oklch } from './colorScience'

/**
 * The palette is one source — `index.css` — and this file asks three questions of it:
 * does `chartPalette.ts` hand out exactly the slots the CSS declares, do the declared values
 * still clear the bars the CSS docblock claims, and do the semantic tokens still equal the
 * Tailwind steps ~70 call sites use. A re-step that breaks any of them fails here.
 */

const CSS = readFileSync(fileURLToPath(new URL('../index.css', import.meta.url)), 'utf8')
type Theme = 'light' | 'dark'
const THEMES: Theme[] = ['light', 'dark']

function token(theme: Theme, name: string): string {
  const found = cssDeclarations(CSS, name)
  if (found.length !== 2) throw new Error(`index.css declares --${name} ${found.length} times, expected 2`)
  return found[theme === 'light' ? 0 : 1]
}

const slots = (theme: Theme) => Array.from({ length: CHART_SLOTS }, (_, i) => token(theme, `chart-${i + 1}`))
const card = (theme: Theme) => hslTripleToHex(token(theme, 'card'))

// The dataviz validator's bars (OKLab ΔE×100, Machado-2009 at severity 1).
const CVD_TARGET = 8
const NORMAL_FLOOR = 15
const BAND: Record<Theme, [number, number]> = { light: [0.43, 0.77], dark: [0.48, 0.67] }
const CHROMA_FLOOR = 0.1
/** How many leading slots must stay apart from EVERY other leading slot, not just neighbours. */
const ALL_PAIRS_LEAD = 3

describe('chartPalette.ts hands out what index.css declares', () => {
  it('has one reference per declared slot, and no slot past the last', () => {
    expect(CHART_PALETTE).toHaveLength(CHART_SLOTS)
    expect(cssDeclarations(CSS, `chart-${CHART_SLOTS + 1}`)).toHaveLength(0)
    CHART_PALETTE.forEach((ref, i) => {
      expect(ref).toBe(`var(--chart-${i + 1})`)
      for (const theme of THEMES) expect(token(theme, `chart-${i + 1}`)).toMatch(/^#[0-9a-f]{6}$/)
    })
    for (const theme of THEMES) expect(token(theme, 'chart-other')).toMatch(/^#[0-9a-f]{6}$/)
  })

  it('folds rather than cycles: an eleventh series is Other, never slot one again', () => {
    expect(chartColor(0)).toBe('var(--chart-1)')
    expect(chartColor(CHART_SLOTS - 1)).toBe(`var(--chart-${CHART_SLOTS})`)
    expect(chartColor(CHART_SLOTS)).toBe(CHART_OTHER)
    expect(chartColor(-1)).toBe(CHART_OTHER)
    expect(chartColor(1.5)).toBe(CHART_OTHER)
    expect(chartColor(Number.NaN)).toBe(CHART_OTHER)
  })

  it('colours a key by its place in the order it is given, and Other when absent', () => {
    const order = ['Technology', 'Financials', 'Healthcare']
    expect(chartColorFor('Financials', order)).toBe('var(--chart-2)')
    expect(chartColorFor('Utilities', order)).toBe(CHART_OTHER)
  })

  it('gives a sign its semantic colour, and an unknown the neutral one', () => {
    expect(signColor(3)).toBe(GAIN_COLOR)
    expect(signColor(-0.01)).toBe(LOSS_COLOR)
    expect(signColor(0)).toBe(NEUTRAL_COLOR)
    expect(signColor(null)).toBe(NEUTRAL_COLOR)
    expect(signColor(Number.NaN)).toBe(NEUTRAL_COLOR)
    expect(signColor(4, 5)).toBe(NEUTRAL_COLOR)
  })
})

describe.each(THEMES)('the %s palette still measures what index.css claims', (theme) => {
  const palette = slots(theme)

  it('keeps every slot in the lightness band and above the chroma floor', () => {
    const [lo, hi] = BAND[theme]
    for (const hex of palette) {
      const { l, c } = oklch(hex)
      expect(l, `${hex} lightness`).toBeGreaterThanOrEqual(lo)
      expect(l, `${hex} lightness`).toBeLessThanOrEqual(hi)
      expect(c, `${hex} chroma`).toBeGreaterThanOrEqual(CHROMA_FLOOR)
    }
  })

  it('separates every adjacent pair, for colour-blind and full-colour readers alike', () => {
    for (let i = 0; i < palette.length - 1; i++) {
      const [a, b] = [palette[i], palette[i + 1]]
      expect(cvdDeltaE(a, b), `slot ${i + 1} vs ${i + 2} (CVD)`).toBeGreaterThanOrEqual(CVD_TARGET)
      expect(deltaE(a, b), `slot ${i + 1} vs ${i + 2}`).toBeGreaterThanOrEqual(NORMAL_FLOOR)
    }
  })

  it(`separates the first ${ALL_PAIRS_LEAD} slots from each other, all pairs`, () => {
    for (let i = 0; i < ALL_PAIRS_LEAD; i++) {
      for (let j = i + 1; j < ALL_PAIRS_LEAD; j++) {
        expect(cvdDeltaE(palette[i], palette[j]), `slot ${i + 1} vs ${j + 1} (CVD)`).toBeGreaterThanOrEqual(CVD_TARGET)
        expect(deltaE(palette[i], palette[j]), `slot ${i + 1} vs ${j + 1}`).toBeGreaterThanOrEqual(NORMAL_FLOOR)
      }
    }
  })

  it('draws every slot visibly on the card', () => {
    // Dark clears 3:1 outright. Light lets three slots sit at 2:1+ (lime, sky, amber on
    // white), which the docblock owns: those charts carry a legend or labels.
    const floor = theme === 'dark' ? 3 : 2
    for (const hex of palette) {
      expect(contrast(hex, card(theme)), `${hex} on the card`).toBeGreaterThanOrEqual(floor)
    }
  })
})

describe('the semantic tokens are the Tailwind steps the app already uses', () => {
  const colors = tailwindConfig.theme.extend.colors as Record<string, Record<string, string>>
  // `text-green-600 dark:text-green-400` is how ~70 call sites write a gain. `text-gain` is
  // the same colour in one class — and only if these stay equal.
  const pairs: [string, string][] = [['gain', 'green'], ['loss', 'red'], ['warn', 'yellow']]

  it.each(pairs)('--%s equals the re-pointed %s-600 (light) and -400 (dark)', (name, scale) => {
    const near = (a: string, b: string) => deltaE(a, b) < 1
    expect(near(hslTripleToHex(token('light', name)), colors[scale]['600'])).toBe(true)
    expect(near(hslTripleToHex(token('dark', name)), colors[scale]['400'])).toBe(true)
  })

  // 4.0 rather than AA's 4.5, and that is arithmetic, not taste: rose-600 itself is 4.7:1 on
  // white, so any visible wash under it lands near 4.2. The well's icon is a graphic (3:1);
  // the pill's figure is the one place the 4.0 applies, and it is never the only copy of it.
  it.each(THEMES)('keeps %s state ink readable on its own soft well', (theme) => {
    for (const name of ['gain', 'loss', 'warn', 'info']) {
      const ink = hslTripleToHex(token(theme, name))
      const well = hslTripleToHex(token(theme, `${name}-soft`))
      expect(contrast(ink, well), `${name} on ${name}-soft`).toBeGreaterThanOrEqual(4)
    }
  })
})
