import { describe, it, expect } from 'vitest'
import {
  buildExposureTiles,
  buildPartition,
  holdingKind,
  unattributedValue,
} from './lookthroughChart'
import type { LookthroughResponse, LookthroughCompanyRow } from './api'

/**
 * The chart builders' one job is to not lie about what the areas mean, so most of these
 * assert the same thing from different sides: the tiles cover the whole portfolio, and the
 * segments do too. Everything else here is a refusal — no chart from an unpriced book, no
 * zero-width tile, no empty legend row.
 */

function company(over: Partial<LookthroughCompanyRow> = {}): LookthroughCompanyRow {
  return {
    company_key: 'K1',
    key_type: 'lei',
    name: 'A COMPANY',
    value_eur: 100,
    pct_of_portfolio: 10,
    direct_value_eur: 100,
    via_funds_value_eur: 0,
    isins: ['X'],
    listings: ['A@B'],
    via_funds: [],
    partially_resolved: false,
    identity_conflicts: [],
    ...over,
  }
}

function response(over: Partial<LookthroughResponse> = {}): LookthroughResponse {
  return {
    as_of: '2026-08-16',
    base_currency: 'EUR',
    total_market_value_eur: 1000,
    direct_equity_eur: 400,
    looked_through_equity_eur: 300,
    fund_residual_eur: 50,
    nested_fund_eur: 25,
    uncovered_fund_eur: 225,
    coverage_pct: 70,
    companies: [
      company({ company_key: 'K1', value_eur: 400, pct_of_portfolio: 40 }),
      company({
        company_key: 'K2',
        name: 'FUND ONLY',
        value_eur: 250,
        pct_of_portfolio: 25,
        direct_value_eur: 0,
        via_funds_value_eur: 250,
      }),
    ],
    companies_shown: 2,
    company_count_total: 3,
    shown_value_eur: 650,
    other_companies_eur: 50,
    other_companies_count: 1,
    funds: [],
    oldest_basket_as_of: null,
    unvaluable_positions: 0,
    unvaluable_symbols: [],
    identity: {
      resolved_by_lei: 2,
      resolved_by_share_class_figi: 0,
      resolved_by_isin: 0,
      unidentified_groups: 0,
      partially_resolved_groups: 0,
      unresolved_isins: 0,
      unresolved_value_eur: 0,
    },
    warnings: [],
    ...over,
  }
}

describe('holdingKind', () => {
  it('separates a company held both ways from one held only one way', () => {
    expect(holdingKind(company({ direct_value_eur: 10, via_funds_value_eur: 0 }))).toBe('direct')
    expect(holdingKind(company({ direct_value_eur: 0, via_funds_value_eur: 10 }))).toBe(
      'via_funds',
    )
    expect(holdingKind(company({ direct_value_eur: 10, via_funds_value_eur: 10 }))).toBe('both')
  })
})

describe('buildExposureTiles', () => {
  it('covers the whole portfolio, so a tile area is a share of the book and not of the covered part', () => {
    const data = response()
    const tiles = buildExposureTiles(data)

    const summed = tiles.reduce((acc, tile) => acc + tile.value, 0)
    expect(summed).toBeCloseTo(data.total_market_value_eur, 6)
    // The same statement as a percentage, which is what the header claims.
    expect(tiles.reduce((acc, tile) => acc + tile.pct, 0)).toBeCloseTo(100, 6)
  })

  it('carries the truncated tail and the unattributed remainder as their own tiles', () => {
    const tiles = buildExposureTiles(response())
    const roles = tiles.map((tile) => tile.role)

    expect(roles).toContain('other_companies')
    expect(roles).toContain('unattributed')
    // 225 uncovered + 50 residual + 25 nested.
    expect(tiles.find((tile) => tile.role === 'unattributed')?.value).toBe(300)
    expect(tiles.find((tile) => tile.role === 'other_companies')?.name).toBe(
      '1 smaller companies',
    )
  })

  it('leaves the structural tiles out when there is nothing for them to carry', () => {
    const tiles = buildExposureTiles(
      response({
        total_market_value_eur: 650,
        direct_equity_eur: 400,
        looked_through_equity_eur: 250,
        fund_residual_eur: 0,
        nested_fund_eur: 0,
        uncovered_fund_eur: 0,
        other_companies_eur: 0,
        other_companies_count: 0,
        coverage_pct: 100,
      }),
    )
    expect(tiles.map((tile) => tile.role)).toEqual(['direct', 'via_funds'])
  })

  it('only company tiles can be clicked through to a breakdown', () => {
    const tiles = buildExposureTiles(response())
    for (const tile of tiles) {
      const structural = tile.role === 'other_companies' || tile.role === 'unattributed'
      expect(tile.companyKey === null).toBe(structural)
    }
  })

  it('drops a zero-valued company rather than seating it in the hover layer', () => {
    const tiles = buildExposureTiles(
      response({
        companies: [
          company({ company_key: 'K1', value_eur: 400, pct_of_portfolio: 40 }),
          company({
            company_key: 'ZERO',
            value_eur: 0,
            pct_of_portfolio: 0,
            direct_value_eur: 0,
            via_funds_value_eur: 0,
          }),
        ],
      }),
    )
    expect(tiles.some((tile) => tile.key === 'ZERO')).toBe(false)
  })

  it('draws nothing at all when no position could be priced', () => {
    // The `concentrationPct` rule: an unpriced book has an unknown shape, and a chart of
    // confident rectangles is a worse answer than no chart.
    expect(buildExposureTiles(response({ total_market_value_eur: 0 }))).toEqual([])
  })
})

describe('buildPartition', () => {
  it('sums to the whole portfolio', () => {
    const data = response()
    const segments = buildPartition(data)

    expect(segments.reduce((acc, s) => acc + s.value, 0)).toBeCloseTo(
      data.total_market_value_eur,
      6,
    )
    expect(segments.reduce((acc, s) => acc + s.pct, 0)).toBeCloseTo(100, 6)
  })

  it('folds the three unattributable buckets into one segment', () => {
    const data = response()
    const unattributed = buildPartition(data).find((s) => s.key === 'unattributed')
    expect(unattributed?.value).toBe(unattributedValue(data))
    expect(unattributed?.value).toBe(300)
  })

  it('omits a segment with nothing in it instead of drawing an empty category', () => {
    const segments = buildPartition(
      response({
        total_market_value_eur: 700,
        direct_equity_eur: 400,
        looked_through_equity_eur: 300,
        fund_residual_eur: 0,
        nested_fund_eur: 0,
        uncovered_fund_eur: 0,
      }),
    )
    expect(segments.map((s) => s.key)).toEqual(['direct', 'via_funds'])
  })

  it('draws nothing when no position could be priced', () => {
    expect(buildPartition(response({ total_market_value_eur: 0 }))).toEqual([])
  })
})
