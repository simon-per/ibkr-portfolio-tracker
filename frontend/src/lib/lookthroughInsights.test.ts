import { describe, it, expect } from 'vitest'
import { lookthroughInsights } from './lookthroughInsights'
import { avatarLabel, listingTickers, logoIsin } from './lookthroughIdentity'
import type { LookthroughCompanyRow, LookthroughResponse } from './api'

function company(over: Partial<LookthroughCompanyRow> = {}): LookthroughCompanyRow {
  return {
    company_key: 'K1',
    key_type: 'lei',
    name: 'A COMPANY',
    sector: 'Technology',
    value_eur: 100,
    pct_of_portfolio: 10,
    direct_value_eur: 100,
    via_funds_value_eur: 0,
    isins: ['US0000000001'],
    listings: ['A@B'],
    via_funds: [],
    partially_resolved: false,
    identity_conflicts: [],
    ...over,
  }
}

function response(companies: LookthroughCompanyRow[], total = 1000): LookthroughResponse {
  return {
    as_of: '2026-08-16',
    base_currency: 'EUR',
    total_market_value_eur: total,
    cash_eur: null,
    cash_source: 'unknown',
    total_value_eur: total,
    cash_pct_of_portfolio: null,
    direct_equity_eur: 0,
    looked_through_equity_eur: 0,
    fund_residual_eur: 0,
    nested_fund_eur: 0,
    uncovered_fund_eur: 0,
    coverage_pct: 70,
    companies,
    companies_shown: companies.length,
    company_count_total: companies.length,
    shown_value_eur: 0,
    other_companies_eur: 0,
    other_companies_count: 0,
    funds: [],
    oldest_basket_as_of: null,
    unvaluable_positions: 0,
    unvaluable_symbols: [],
    identity: {
      resolved_by_lei: 0, resolved_by_share_class_figi: 0, resolved_by_isin: 0,
      unidentified_groups: 0, partially_resolved_groups: 0, unresolved_isins: 0, unresolved_value_eur: 0,
    },
    warnings: [],
  }
}

describe('lookthroughInsights', () => {
  it('names the largest exposure, by value rather than by the order rows arrive in', () => {
    const out = lookthroughInsights(response([
      company({ company_key: 'S', value_eur: 50, pct_of_portfolio: 5 }),
      company({ company_key: 'L', name: 'LARGE', value_eur: 300, pct_of_portfolio: 30 }),
    ]))
    expect(out.largest?.companyKey).toBe('L')
    expect(out.largest?.pct).toBe(30)
  })

  it('sums the top N as shares of the whole book, never renormalised onto the covered part', () => {
    // Two companies covering 40% of the book: the top-10 figure is 40%, not 100%.
    const out = lookthroughInsights(response([
      company({ company_key: 'A', value_eur: 300, pct_of_portfolio: 30 }),
      company({ company_key: 'B', value_eur: 100, pct_of_portfolio: 10 }),
    ]))
    expect(out.top).toEqual({ count: 2, pct: 40 })
  })

  it('takes only the first N when there are more', () => {
    const rows = Array.from({ length: 12 }, (_, i) =>
      company({ company_key: `K${String(i).padStart(2, '0')}`, value_eur: 120 - i, pct_of_portfolio: 1 }))
    expect(lookthroughInsights(response(rows)).top).toEqual({ count: 10, pct: 10 })
    expect(lookthroughInsights(response(rows), 3).top).toEqual({ count: 3, pct: 3 })
  })

  it('finds the largest company reached only through funds', () => {
    const out = lookthroughInsights(response([
      company({ company_key: 'D', value_eur: 500, direct_value_eur: 500 }),
      company({ company_key: 'F2', value_eur: 80, direct_value_eur: 0, via_funds_value_eur: 80 }),
      company({ company_key: 'F1', value_eur: 90, direct_value_eur: 0, via_funds_value_eur: 90 }),
    ]))
    expect(out.largestViaFunds?.companyKey).toBe('F1')
  })

  it('is absent, not zero, when nothing is reached only through funds', () => {
    const out = lookthroughInsights(response([company({ direct_value_eur: 100, via_funds_value_eur: 0 })]))
    expect(out.largestViaFunds).toBeNull()
  })

  it('counts companies held both ways and what the funds add', () => {
    const out = lookthroughInsights(response([
      company({ company_key: 'B1', value_eur: 300, direct_value_eur: 250, via_funds_value_eur: 50 }),
      company({ company_key: 'B2', value_eur: 100, direct_value_eur: 70, via_funds_value_eur: 30 }),
      company({ company_key: 'D', value_eur: 90, direct_value_eur: 90, via_funds_value_eur: 0 }),
    ]))
    expect(out.heldBothWays).toEqual({ count: 2, viaFundsValue: 80 })
  })

  it('refuses every figure for an unpriced book or an empty ranking', () => {
    const none = { largest: null, top: null, largestViaFunds: null, heldBothWays: null }
    expect(lookthroughInsights(response([company()], 0))).toEqual(none)
    expect(lookthroughInsights(response([]))).toEqual(none)
    expect(lookthroughInsights(response([company({ value_eur: 0 })]))).toEqual(none)
  })

  it('breaks a value tie on the key, so equal rows cannot swap between renders', () => {
    const rows = [company({ company_key: 'Z', value_eur: 100 }), company({ company_key: 'A', value_eur: 100 })]
    expect(lookthroughInsights(response(rows)).largest?.companyKey).toBe('A')
    expect(lookthroughInsights(response([...rows].reverse())).largest?.companyKey).toBe('A')
  })
})

describe('lookthroughIdentity', () => {
  it('strips the exchange from listing keys and de-duplicates', () => {
    expect(listingTickers(['GOOG@NASDAQ', 'GOOGL@NASDAQ', 'GOOG@EBS'])).toEqual(['GOOG', 'GOOGL'])
  })

  it('asks for the first ISIN that has a stored logo, else the first ISIN', () => {
    expect(logoIsin(['US1', 'us2'], { US2: 'v1' })).toBe('US2')
    expect(logoIsin(['US1', 'US2'], {})).toBe('US1')
    expect(logoIsin([], { US2: 'v1' })).toBeNull()
    // A manifest is a plain object: a prototype key is not a logo.
    expect(logoIsin(['CONSTRUCTOR'], {})).toBe('CONSTRUCTOR')
  })

  it('spells the monogram from the first ticker, else the name', () => {
    expect(avatarLabel(['NVDA@NASDAQ'], 'NVIDIA')).toBe('NVDA')
    expect(avatarLabel([], 'NVIDIA')).toBe('NVIDIA')
  })
})
