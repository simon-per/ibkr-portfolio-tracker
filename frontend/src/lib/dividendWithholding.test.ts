import { describe, expect, it } from 'vitest'

import { formatDividendWithholdingPct } from './dividendWithholding'

describe('formatDividendWithholdingPct', () => {
  it('prints a measured rate without trailing zeros', () => {
    expect(formatDividendWithholdingPct(15)).toBe('15')
    expect(formatDividendWithholdingPct(21.0)).toBe('21')
    expect(formatDividendWithholdingPct(26.375)).toBe('26.375')
  })
})
