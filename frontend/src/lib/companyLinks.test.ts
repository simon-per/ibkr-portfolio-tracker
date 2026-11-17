import { describe, expect, it } from 'vitest'
import { NO_COMPANY_LINKS, companyKeyFor, companyLinks } from './companyLinks'
import { FIXTURE_COMPANIES, AAPL_KEY as AAPL_CIK, NVDA_KEY as NVDA_CIK } from './companyFinancials.fixture'

const ALPHABET_CIK = 'sec:1652044'
const ALPHABET = {
  ...FIXTURE_COMPANIES[1],
  key: ALPHABET_CIK,
  cik: 1652044,
  security_id: 31,
  security_ids: [31, 32],
  symbol: 'GOOG',
  name: 'Alphabet Inc',
}

describe('companyLinks', () => {
  const links = companyLinks([...FIXTURE_COMPANIES, ALPHABET])

  it('finds a held company from any of its listings', () => {
    expect(companyKeyFor(links, { securityId: 11 })).toBe(NVDA_CIK)
    expect(companyKeyFor(links, { securityId: 31 })).toBe(ALPHABET_CIK)
    expect(companyKeyFor(links, { securityId: 32 })).toBe(ALPHABET_CIK)
  })

  it('finds a company by ticker, whatever its case, when the security says nothing', () => {
    expect(companyKeyFor(links, { ticker: 'aapl' })).toBe(AAPL_CIK)
    expect(companyKeyFor(links, { securityId: 999, ticker: ' NVDA ' })).toBe(NVDA_CIK)
  })

  it("finds a company by any of its ISINs, before the ticker", () => {
    expect(companyKeyFor(links, { isin: 'us0378331005' })).toBe(AAPL_CIK)
    expect(companyKeyFor(links, { isins: ['IE00B4L5Y983', 'US0378331005'] })).toBe(AAPL_CIK)
    expect(companyKeyFor(links, { isin: 'US0378331005', ticker: 'NVDA' })).toBe(AAPL_CIK)
    expect(companyKeyFor(links, { isins: ['IE00B4L5Y983'] })).toBeNull()
  })

  it('prefers the security over the ticker', () => {
    expect(companyKeyFor(links, { securityId: 11, ticker: 'AAPL' })).toBe(NVDA_CIK)
  })

  it('links nothing it cannot find, and nothing at all without the list', () => {
    expect(companyKeyFor(links, { securityId: 999 })).toBeNull()
    expect(companyKeyFor(links, { ticker: 'MSFT' })).toBeNull()
    expect(companyKeyFor(links, {})).toBeNull()
    expect(companyKeyFor(NO_COMPANY_LINKS, { securityId: 11, ticker: 'NVDA' })).toBeNull()
  })
})
