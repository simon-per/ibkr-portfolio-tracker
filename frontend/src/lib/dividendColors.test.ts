import { describe, expect, it } from 'vitest'
import { MAX_SERIES, OTHER } from './dividendChart'
import { dividendColor, dividendPalette, OTHER_COLOR, SERIES_DARK, SERIES_LIGHT } from './dividendColors'

/**
 * The slot count and the palettes that fill them used to live in two files with
 * nothing linking them. Raising `MAX_SERIES` alone would have handed the extra
 * symbols `palette[n] ?? OTHER_COLOR` — grey, indistinguishable from the Other
 * bucket, with several identical swatches in the legend and nothing failing.
 *
 * So this is written as the family question ("can every slot be told apart?")
 * rather than the instance one ("are these two numbers equal?"). The cheap pin is
 * kept alongside because it names the cause when the family assertion trips.
 */
describe('the dividend palette covers every slot the ranking can fill', () => {
  it('has exactly one colour per series slot', () => {
    expect(SERIES_LIGHT).toHaveLength(MAX_SERIES)
    expect(SERIES_DARK).toHaveLength(MAX_SERIES)
  })

  for (const theme of ['light', 'dark']) {
    it(`gives every ranked symbol a distinct colour in ${theme}`, () => {
      const palette = dividendPalette(theme)
      // More symbols than slots, so `Other` is present — exactly what the chart
      // hands this function.
      const stackSymbols = [
        ...Array.from({ length: MAX_SERIES }, (_, i) => `S${i}`),
        OTHER,
      ]
      const colors = stackSymbols.map((s) => dividendColor(s, stackSymbols, palette))

      expect(colors.every(Boolean)).toBe(true)
      expect(new Set(colors).size).toBe(colors.length)
      expect(colors.filter((c) => c === OTHER_COLOR)).toHaveLength(1)
      // The Other grey must not also be a series colour, or the bucket and a real
      // holding would be the same swatch.
      expect(palette).not.toContain(OTHER_COLOR)
    })
  }

  it('falls back to the Other grey for a symbol outside the stack', () => {
    // DividendCalendar colours an upcoming payment whose symbol may not have
    // earned a slot. Grey is the honest answer there; throwing is not.
    expect(dividendColor('NOPE', ['AAA'], SERIES_LIGHT)).toBe(OTHER_COLOR)
  })
})
