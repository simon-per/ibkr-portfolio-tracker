import { describe, expect, it } from 'vitest'

import { forecastMethodLabel, formatDividendWithholdingPct } from './dividendWithholding'

describe('forecastMethodLabel', () => {
  it('names every sizing method the backend sends', () => {
    expect(forecastMethodLabel('announced')).toBe('declared')
    expect(forecastMethodLabel('latest_payment')).toBe('latest')
    expect(forecastMethodLabel('same_payment_last_year')).toBe('as last yr')
    expect(forecastMethodLabel('estimate')).toBe('recorded')
  })

  it('says nothing rather than guess for an absent or unknown method', () => {
    expect(forecastMethodLabel(null)).toBeNull()
    expect(forecastMethodLabel(undefined)).toBeNull()
    expect(forecastMethodLabel('median8')).toBeNull()
  })
})

describe('formatDividendWithholdingPct', () => {
  it('prints a measured rate without trailing zeros', () => {
    expect(formatDividendWithholdingPct(15)).toBe('15')
    expect(formatDividendWithholdingPct(21.0)).toBe('21')
    expect(formatDividendWithholdingPct(26.375)).toBe('26.375')
  })
})
