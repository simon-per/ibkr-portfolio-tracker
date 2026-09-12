import { describe, expect, it } from 'vitest'
import {
  cn,
  currencySymbol,
  formatCurrency,
  formatDate,
  formatPercent,
  formatShortDateTime,
  parseLocalDate,
  tooltipValue,
} from './utils'

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
    expect(formatShortDateTime('2026-08-09T04:00:00+00:00')).toMatch(
      /^\d{1,2}\/\d{1,2}\/\d{2}, \d{1,2}:\d{2}\s(AM|PM)$/,
    )
  })

  it('accepts a Date as given', () => {
    // `\s` rather than a literal space: recent ICU separates the meridiem with U+202F.
    expect(formatShortDateTime(new Date(2026, 7, 9, 6, 0))).toMatch(/^8\/9\/26, 6:00\sAM$/)
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
    expect(cn('a', false && 'b', undefined, 'c')).toBe('a c')
  })

  it('lets a later Tailwind class win over an earlier conflicting one', () => {
    // The reason this wraps twMerge rather than clsx alone.
    expect(cn('p-2', 'p-4')).toBe('p-4')
  })
})
