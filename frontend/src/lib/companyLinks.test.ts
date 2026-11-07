import { describe, expect, it } from 'vitest'
import { NO_COMPANY_LINKS, companyCikFor, companyLinks } from './companyLinks'
import { FIXTURE_COMPANIES, AAPL_CIK, NVDA_CIK } from './companyFinancials.fixture'

const ALPHABET_CIK = 1652044
const ALPHABET = {
  ...FIXTURE_COMPANIES[1],
  cik: ALPHABET_CIK,
  security_id: 31,
  security_ids: [31, 32],
  symbol: 'GOOG',
  name: 'Alphabet Inc',
}

describe('companyLinks', () => {
  const links = companyLinks([...FIXTURE_COMPANIES, ALPHABET])

  it('finds a held company from any of its listings', () => {
    expect(companyCikFor(links, { securityId: 11 })).toBe(NVDA_CIK)
    expect(companyCikFor(links, { securityId: 31 })).toBe(ALPHABET_CIK)
    expect(companyCikFor(links, { securityId: 32 })).toBe(ALPHABET_CIK)
  })

  it('finds a company by ticker, whatever its case, when the security says nothing', () => {
    expect(companyCikFor(links, { ticker: 'aapl' })).toBe(AAPL_CIK)
    expect(companyCikFor(links, { securityId: 999, ticker: ' NVDA ' })).toBe(NVDA_CIK)
  })

  it("finds a company by any of its ISINs, before the ticker", () => {
    expect(companyCikFor(links, { isin: 'us0378331005' })).toBe(AAPL_CIK)
    expect(companyCikFor(links, { isins: ['IE00B4L5Y983', 'US0378331005'] })).toBe(AAPL_CIK)
    expect(companyCikFor(links, { isin: 'US0378331005', ticker: 'NVDA' })).toBe(AAPL_CIK)
    expect(companyCikFor(links, { isins: ['IE00B4L5Y983'] })).toBeNull()
  })

  it('prefers the security over the ticker', () => {
    expect(companyCikFor(links, { securityId: 11, ticker: 'AAPL' })).toBe(NVDA_CIK)
  })

  it('links nothing it cannot find, and nothing at all without the list', () => {
    expect(companyCikFor(links, { securityId: 999 })).toBeNull()
    expect(companyCikFor(links, { ticker: 'MSFT' })).toBeNull()
    expect(companyCikFor(links, {})).toBeNull()
    expect(companyCikFor(NO_COMPANY_LINKS, { securityId: 11, ticker: 'NVDA' })).toBeNull()
  })
})
