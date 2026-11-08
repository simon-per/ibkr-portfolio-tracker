import { describe, expect, it } from 'vitest'
import { fundLabel } from './lookthroughIdentity'

describe('fundLabel (review item L7)', () => {
  it('names a fund by its symbol', () => {
    expect(fundLabel({ symbol: 'VWCE', name: 'Vanguard FTSE All-World', fund_isin: 'IE00BK5BQT80' })).toBe('VWCE')
  })

  it('names a fund whose symbol is its ISIN by its name', () => {
    const fund = {
      symbol: 'CH0117044948',
      name: 'SWISSCANTO (CH) IPF I INDEX EQUITY FUND WORLD EX CH NT CHF',
      fund_isin: 'CH0117044948',
    }
    expect(fundLabel(fund)).not.toBe('CH0117044948')
    expect(fundLabel(fund).toLowerCase()).toContain('swisscanto')
  })

  it('falls back to the ISIN with nothing better', () => {
    expect(fundLabel({ symbol: null, name: null, fund_isin: 'CH1529078078' })).toBe('CH1529078078')
  })
})
