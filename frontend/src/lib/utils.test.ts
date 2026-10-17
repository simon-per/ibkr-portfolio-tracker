import { describe, expect, it } from 'vitest'
import {
  formatAxisMoney,
  cn,
  currencySymbol,
  formatCurrency,
  formatDate,
  formatPercent,
  formatPrice,
  formatQuantity,
  formatShortDateTime,
  parseLocalDate,
  tooltipValue,
  formatMarketCap,
  marketCapParts,
  ABSENT,
} from './utils'

describe('formatMarketCap', () => {
  it('draws the app-wide absent glyph for an unknown, never a hyphen', () => {
    expect(formatMarketCap(null)).toBe(ABSENT)
    expect(ABSENT).toBe('—')
  })

  it('keeps its T / B / M scale', () => {
    expect(formatMarketCap(2.5e12)).toBe('2.5T')
    expect(formatMarketCap(850_000)).toBe('850,000')
  })
})

describe('marketCapParts', () => {
  it('prints the base-currency figure under the base prefix, with no native code', () => {
    expect(marketCapParts(
      { market_cap: 1.2e15, market_cap_base: 8.1e11, data_currency: 'KRW' }, 'CHF ',
    )).toEqual({ figure: 'CHF 810.0B', code: null })
  })

  it('falls back to the listing’s own figure, labelled, never under the base prefix', () => {
    expect(marketCapParts(
      { market_cap: 9e11, market_cap_base: null, data_currency: 'GBp' }, 'CHF ',
    )).toEqual({ figure: '900.0B', code: 'GBp' })
  })

  it('is absent when there is no figure at all', () => {
    expect(marketCapParts(
      { market_cap: null, market_cap_base: null, data_currency: null }, '€',
    )).toEqual({ figure: ABSENT, code: null })
  })
})

/**
 * `lib/utils.ts` had no tests, and it holds the money formatter every surface
 * uses plus the date formatter the portfolio chart labels its points with.
 *
 * The date half was wrong. `new Date("2026-03-14")` parses a date-only string as
 * UTC midnight, which `Intl` then renders in local time — one day early anywhere
 * west of Greenwich, for every date the API sends, since the API speaks date-only
 * strings throughout.
 */

describe('parseLocalDate', () => {
  it('reads a date-only string on the local calendar, not as UTC', () => {
    const d = parseLocalDate('2026-03-14')
    expect(d.getFullYear()).toBe(2026)
    expect(d.getMonth()).toBe(2)
    expect(d.getDate()).toBe(14)
  })

  it('lands on local midnight', () => {
    // The sharpest single assertion: UTC-midnight parsing shows up here as an
    // hour offset in every zone that is not itself UTC.
    const d = parseLocalDate('2026-03-14')
    expect(d.getHours()).toBe(0)
    expect(d.getMinutes()).toBe(0)
  })

  it('differs from the stdlib UTC parse whenever the host is not on UTC', () => {
    // Guards the fix rather than the helper: a revert passes vacuously on a UTC
    // machine and fails everywhere else, which is exactly the blind spot.
    const offsetMinutes = new Date('2026-03-14').getTimezoneOffset()
    if (offsetMinutes === 0) return // host is on UTC; the two are indistinguishable
    expect(parseLocalDate('2026-03-14').getTime()).not.toBe(new Date('2026-03-14').getTime())
  })

  it('leaves a full timestamp to stdlib parsing', () => {
    // A timestamp names a real instant, so converting it to local time is right.
    const iso = '2026-03-14T15:30:00Z'
    expect(parseLocalDate(iso).getTime()).toBe(new Date(iso).getTime())
  })

  it('does not mistake a partial date for a date-only string', () => {
    expect(Number.isNaN(parseLocalDate('2026-03').getTime())).toBe(
      Number.isNaN(new Date('2026-03').getTime()),
    )
  })
})

describe('formatDate', () => {
  it('names the day the string says, in every timezone', () => {
    expect(formatDate('2026-03-14')).toBe('Mar 14, 2026')
  })

  it('does not shift a new-year boundary into the previous year', () => {
    // The worst case of the old behaviour: 1 January read as 31 December.
    expect(formatDate('2026-01-01')).toBe('Jan 1, 2026')
  })

  it('handles the end of a month', () => {
    expect(formatDate('2026-12-31')).toBe('Dec 31, 2026')
  })

  it('handles a leap day', () => {
    expect(formatDate('2024-02-29')).toBe('Feb 29, 2024')
  })

  it('accepts a Date as given', () => {
    expect(formatDate(new Date(2026, 2, 14))).toBe('Mar 14, 2026')
  })
})

describe('formatCurrency', () => {
  it('defaults to EUR and always shows two decimals', () => {
    expect(formatCurrency(1234.5)).toBe('€1,234.50')
  })

  it('formats the three base currencies the app supports', () => {
    expect(formatCurrency(1000, 'USD')).toBe('$1,000.00')
    expect(formatCurrency(1000, 'CHF')).toBe('CHF\u00a01,000.00')
  })

  it('keeps a negative sign, which withdrawals and losses depend on', () => {
    expect(formatCurrency(-800, 'EUR')).toBe('-€800.00')
  })

  it('puts the sign before a word-length code too — the app-wide convention', () => {
    expect(formatCurrency(-800, 'CHF')).toBe('-CHF 800.00')
  })

  it('rounds rather than truncating', () => {
    expect(formatCurrency(0.005)).toBe('€0.01')
    expect(formatCurrency(0.004)).toBe('€0.00')
  })

  it('does not collapse a large figure to a compact form', () => {
    expect(formatCurrency(1234567.89)).toBe('€1,234,567.89')
  })
})

describe('currencySymbol', () => {
  it('gives a tight symbol for the currencies with one', () => {
    expect(currencySymbol('EUR')).toBe('€')
    expect(currencySymbol('USD')).toBe('$')
  })

  it('separates CHF from its amount with a non-breaking space', () => {
    // Written as an escape so the invisible character is visible in the test.
    expect(currencySymbol('CHF')).toBe('CHF\u00a0')
  })

  it('falls back to the code plus the same non-breaking space', () => {
    // Never empty: an unmapped currency must not render as a bare number.
    expect(currencySymbol('GBP')).toBe('GBP\u00a0')
    expect(currencySymbol('TWD')).toBe('TWD\u00a0')
  })

  it('defaults to EUR', () => {
    expect(currencySymbol()).toBe('€')
  })
})

describe('formatPercent', () => {
  it('takes a percent-valued number, not a fraction', () => {
    // The API sends 12.5 to mean 12.5%.
    expect(formatPercent(12.5)).toBe('12.50%')
  })

  it('keeps the sign', () => {
    expect(formatPercent(-3.75)).toBe('-3.75%')
  })

  it('shows a small non-zero value rather than rounding it away entirely', () => {
    expect(formatPercent(0.01)).toBe('0.01%')
  })
})

describe('formatShortDateTime', () => {
  it('renders the en-US short form whatever the host locale', () => {
    // Local time, so the digits depend on the host zone; the SHAPE does not. The three
    // sites this replaces passed `undefined` as the locale and followed the runtime —
    // "09.08.26, 06:00" on a German machine for the same instant.
    expect(formatShortDateTime('2026-08-09T04:00:00+00:00', new Date(2026, 9, 1))).toMatch(
      /^Aug \d{1,2}, \d{1,2}:\d{2}\s(AM|PM)$/,
    )
  })

  it('accepts a Date as given', () => {
    // `\s` rather than a literal space: recent ICU separates the meridiem with U+202F.
    expect(formatShortDateTime(new Date(2026, 7, 9, 6, 0), new Date(2026, 9, 1))).toMatch(
      /^Aug 9, 6:00\sAM$/,
    )
  })

  it('names the year only when it is not the current one', () => {
    expect(formatShortDateTime(new Date(2025, 11, 30, 18, 5), new Date(2026, 0, 2))).toMatch(
      /^Dec 30, 2025, 6:05\sPM$/,
    )
  })
})

describe('tooltipValue', () => {
  const money = (v: number) => `€${v.toFixed(2)}`

  it('formats a present value', () => {
    expect(tooltipValue(1234.5, money)).toBe('€1234.50')
  })

  it('returns null — the value Recharts drops a row for — when the point has none', () => {
    // Not '' and not a formatted zero: `formatCurrency(null)` coerced to 0 and printed
    // "€0.00" for a benchmark line that was not drawn on that date.
    expect(tooltipValue(null, money)).toBeNull()
    expect(tooltipValue(undefined, money)).toBeNull()
  })

  it('does not read a real zero as absent', () => {
    expect(tooltipValue(0, money)).toBe('€0.00')
  })
})

describe('cn', () => {
  it('joins class names', () => {
    expect(cn('a', 'b')).toBe('a b')
  })

  it('drops falsy entries', () => {
    expect(cn('a', false, null, undefined, 'c')).toBe('a c')
  })

  it('lets a later Tailwind class win over an earlier conflicting one', () => {
    // The reason this wraps twMerge rather than clsx alone.
    expect(cn('p-2', 'p-4')).toBe('p-4')
  })
})

describe('formatPrice', () => {
  it('is formatCurrency from 1 up', () => {
    expect(formatPrice(60_000, 'USD')).toBe(formatCurrency(60_000, 'USD'))
    expect(formatPrice(1, 'EUR')).toBe('€1.00')
  })

  it('keeps the significant digits of a sub-unit price instead of printing 0.00', () => {
    // A token worth 0.0000123 is cheap, not worthless — "$0.00" says worthless.
    expect(formatPrice(0.0000123, 'USD')).toBe('$0.0000123')
    expect(formatPrice(0.5, 'USD')).toBe('$0.50')
    expect(formatPrice(0.123456789, 'USD')).toBe('$0.123457')
  })

  it('stays pinned to en-US in every currency', () => {
    expect(formatPrice(0.25, 'CHF')).toBe('CHF\u00a00.25')
    expect(formatPrice(0, 'EUR')).toBe('€0.00')
  })
})

describe('formatQuantity', () => {
  it('prints no more digits than a holding needs', () => {
    expect(formatQuantity(5)).toBe('5')
    expect(formatQuantity(0.01)).toBe('0.01')
    expect(formatQuantity(1_000_000)).toBe('1,000,000')
    expect(formatQuantity(12.345678)).toBe('12.3457')
  })

  it('keeps a small balance visible rather than rounding it to 0', () => {
    expect(formatQuantity(0.00000123)).toBe('0.00000123')
    expect(formatQuantity(0.000123456789)).toBe('0.000123457')
  })
})

describe('formatAxisMoney', () => {
  it('scales to k, M and B with at most one decimal and no trailing .0', () => {
    expect(formatAxisMoney(150_000, 'CHF\u00a0')).toBe('CHF\u00a0150k')
    expect(formatAxisMoney(12_500, '€')).toBe('€12.5k')
    expect(formatAxisMoney(1_000, '€')).toBe('€1k')
    expect(formatAxisMoney(1_000_000, 'CHF\u00a0')).toBe('CHF\u00a01M')
    expect(formatAxisMoney(1_250_000, '$')).toBe('$1.3M')
    expect(formatAxisMoney(2_000_000_000, '$')).toBe('$2B')
  })

  it('prints zero as zero, not "0k"', () => {
    expect(formatAxisMoney(0, 'CHF\u00a0')).toBe('CHF\u00a00')
    expect(formatAxisMoney(-0.2, '€')).toBe('€0')
  })

  it('keeps small amounts whole and puts the sign before the symbol', () => {
    expect(formatAxisMoney(950, '€')).toBe('€950')
    expect(formatAxisMoney(-2_500, 'CHF\u00a0')).toBe('-CHF\u00a02.5k')
    expect(formatAxisMoney(-40, '$')).toBe('-$40')
  })

  it('moves up a unit when rounding reaches a thousand', () => {
    expect(formatAxisMoney(999_950, '€')).toBe('€1M')
    expect(formatAxisMoney(999.6)).toBe('1k')
  })

  it('omits the symbol when none is given', () => {
    expect(formatAxisMoney(4_500)).toBe('4.5k')
  })
})
