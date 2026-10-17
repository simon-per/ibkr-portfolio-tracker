import { describe, expect, it } from 'vitest'
import { deltaArrow, deltaColor, deltaTone, formatDelta, FLAT_BAND_PCT } from './delta'

describe('formatDelta', () => {
  it('renders a dash rather than a number when there is no base', () => {
    // The backend sends null when the previous period was zero — which happens
    // constantly, because a quarterly payer pays nothing in most months. Growth
    // against zero is undefined, not large, and must never look like a figure.
    expect(formatDelta(null)).toBe('—')
    expect(formatDelta(undefined)).toBe('—')
  })

  it('never renders a non-finite value as a percentage', () => {
    expect(formatDelta(Infinity)).toBe('—')
    expect(formatDelta(-Infinity)).toBe('—')
    expect(formatDelta(NaN)).toBe('—')
  })

  it('signs positive changes and keeps one decimal below 100%', () => {
    expect(formatDelta(12.34)).toBe('+12.3%')
    expect(formatDelta(-7.56)).toBe('-7.6%')
    expect(formatDelta(0)).toBe('0%')
  })

  it('drops the decimal on large changes, where it is false precision', () => {
    // The real account shows +1300% year-on-year off a part-year base; a tenth
    // of a percent there is noise pretending to be accuracy.
    expect(formatDelta(1321.8)).toBe('+1322%')
    expect(formatDelta(-596.14)).toBe('-596%')
  })

  it('treats exactly 100 as large', () => {
    expect(formatDelta(100)).toBe('+100%')
    expect(formatDelta(99.94)).toBe('+99.9%')
  })
})

describe('deltaArrow', () => {
  it('stays silent inside the flat band, where direction is noise', () => {
    expect(deltaArrow(FLAT_BAND_PCT)).toBe('')
    expect(deltaArrow(-FLAT_BAND_PCT)).toBe('')
    expect(deltaArrow(0)).toBe('')
    expect(deltaArrow(null)).toBe('')
  })

  it('points the way outside it', () => {
    expect(deltaArrow(FLAT_BAND_PCT + 0.1)).toBe('▲')
    expect(deltaArrow(-FLAT_BAND_PCT - 0.1)).toBe('▼')
  })
})

describe('deltaColor', () => {
  it('colours only moves outside the flat band', () => {
    expect(deltaColor(0)).toContain('muted-foreground')
    expect(deltaColor(FLAT_BAND_PCT)).toContain('muted-foreground')
    expect(deltaColor(FLAT_BAND_PCT + 1)).toContain('green')
    expect(deltaColor(-FLAT_BAND_PCT - 1)).toContain('red')
  })

  it('carries a dark step for the literal colours', () => {
    // The chips sit on both card surfaces, and a fixed green or red that works
    // on white fails contrast on #020817. The flat state needs no variant — it
    // uses the semantic token, which already follows the theme.
    expect(deltaColor(10)).toMatch(/dark:/)
    expect(deltaColor(-10)).toMatch(/dark:/)
    expect(deltaColor(0)).toBe('text-muted-foreground')
  })
})

describe('the flat band is a parameter, not a universal claim', () => {
  it('defaults to the band that suits a lumpy series', () => {
    // Dividend income and monthly contributions: a payer shifting its pay date by a
    // few days moves a month by more than this.
    expect(deltaColor(3)).toContain('muted-foreground')
    expect(deltaArrow(3)).toBe('')
  })

  it('lets a caller opt out where every move is signal', () => {
    // A portfolio's period return: muting a 3% quarter as "flat" understates it.
    expect(deltaColor(3, 0)).toContain('green')
    expect(deltaColor(-3, 0)).toContain('red')
    expect(deltaArrow(3, 0)).toBe('▲')
    expect(deltaArrow(-3, 0)).toBe('▼')
  })

  it('still treats exactly zero as flat with no band', () => {
    // Strictly greater / strictly less, so no move is neither up nor down.
    expect(deltaColor(0, 0)).toBe('text-muted-foreground')
    expect(deltaArrow(0, 0)).toBe('')
  })

  it('accepts a wider band too', () => {
    expect(deltaColor(8, 10)).toContain('muted-foreground')
    expect(deltaArrow(12, 10)).toBe('▲')
  })
})

describe('deltaTone', () => {
  it('is the one decision the arrow and the colour both read', () => {
    for (const pct of [-12, -FLAT_BAND_PCT, 0, FLAT_BAND_PCT, FLAT_BAND_PCT + 0.1, 40]) {
      const tone = deltaTone(pct)
      expect(deltaArrow(pct)).toBe(tone === 'up' ? '▲' : tone === 'down' ? '▼' : '')
      expect(deltaColor(pct)).toContain(tone === 'up' ? 'green' : tone === 'down' ? 'red' : 'muted')
    }
  })

  it('reads an unknown as flat, never as a direction', () => {
    expect(deltaTone(null)).toBe('flat')
    expect(deltaTone(undefined)).toBe('flat')
    expect(deltaTone(Number.NaN)).toBe('flat')
  })

  it('honours a zero band, where every move is a direction', () => {
    expect(deltaTone(0.5, 0)).toBe('up')
    expect(deltaTone(-0.5, 0)).toBe('down')
    expect(deltaTone(0, 0)).toBe('flat')
  })
})
