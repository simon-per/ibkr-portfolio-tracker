import { describe, it, expect } from 'vitest'
import {
  allTiles,
  buildExposureGroups,
  buildPartition,
  unattributedValue,
} from './lookthroughChart'
import { OTHER_SECTORS_LABEL } from './sectorColors'
import type { LookthroughResponse, LookthroughCompanyRow } from './api'

/**
 * The chart builders' one job is to not lie about what the areas mean, so most of these assert
 * the same thing from different sides: the leaves cover the whole portfolio, and the segments do
 * too. Everything else here is a refusal — no chart from an unpriced book, no zero-width tile,
 * no empty legend row, and no company silently dropped for want of a sector.
 */

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
        sector: 'Financials',
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

describe('buildExposureGroups', () => {
  it('covers the whole portfolio, so a leaf area is a share of the book and not of the covered part', () => {
    const data = response()
    const leaves = allTiles(buildExposureGroups(data))

    expect(leaves.reduce((acc, t) => acc + t.value, 0)).toBeCloseTo(
      data.total_market_value_eur,
      6,
    )
    expect(leaves.reduce((acc, t) => acc + t.pct, 0)).toBeCloseTo(100, 6)
  })

  it('the groups sum to the whole too, so the two levels cannot disagree', () => {
    const data = response()
    const groups = buildExposureGroups(data)
    expect(groups.reduce((acc, g) => acc + g.value, 0)).toBeCloseTo(
      data.total_market_value_eur,
      6,
    )
    for (const group of groups) {
      expect(group.value).toBeCloseTo(
        group.children.reduce((acc, t) => acc + t.value, 0),
        6,
      )
    }
  })

  it('clusters companies under their sector', () => {
    const groups = buildExposureGroups(response())
    const tech = groups.find((g) => g.name === 'Technology')
    const fin = groups.find((g) => g.name === 'Financials')

    expect(tech?.children.map((c) => c.key)).toEqual(['K1'])
    expect(fin?.children.map((c) => c.key)).toEqual(['K2'])
  })

  it('folds a sector with no hue of its own into one Other sectors group', () => {
    // Healthcare is a real canonical sector and deliberately has no colour slot — only four
    // hues clear the colourblind gate all-pairs, so everything past the first four folds.
    const groups = buildExposureGroups(
      response({
        companies: [
          company({ company_key: 'K1', sector: 'Healthcare', value_eur: 400, pct_of_portfolio: 40 }),
          company({ company_key: 'K2', sector: 'Utilities', value_eur: 250, pct_of_portfolio: 25 }),
        ],
      }),
    )
    const other = groups.find((g) => g.name === OTHER_SECTORS_LABEL)
    expect(other?.children.map((c) => c.key).sort()).toEqual(['K1', 'K2'])
    expect(other?.value).toBe(650)
  })

  it('keeps each company its real sector for the tooltip, not the group it folded into', () => {
    // Otherwise the fold reads as missing data: a healthcare company inside "Other sectors"
    // with no way to say what it is.
    const groups = buildExposureGroups(
      response({
        companies: [company({ company_key: 'K1', sector: 'Healthcare', value_eur: 400 })],
      }),
    )
    const tile = allTiles(groups).find((t) => t.key === 'K1')
    expect(tile?.sector).toBe('Healthcare')
  })

  it('puts a company nothing classified into Other sectors rather than dropping it', () => {
    // Vanguard publishes no sector on any of its 10,032 rows, so Unknown is the common case.
    // Dropping those would shrink the whole and make every other share read high.
    const data = response({
      companies: [
        company({ company_key: 'K1', sector: 'Unknown', value_eur: 400, pct_of_portfolio: 40 }),
        company({ company_key: 'K2', sector: 'Technology', value_eur: 250, pct_of_portfolio: 25 }),
      ],
    })
    const groups = buildExposureGroups(data)
    expect(allTiles(groups).some((t) => t.key === 'K1')).toBe(true)
    expect(allTiles(groups).reduce((acc, t) => acc + t.value, 0)).toBeCloseTo(1000, 6)
  })

  it('carries the truncated tail and the unattributed remainder as their own top-level groups', () => {
    const groups = buildExposureGroups(response())
    const names = groups.map((g) => g.name)

    expect(names).toContain('1 smaller companies')
    expect(names).toContain('Not attributed to a company')
    // 225 uncovered + 50 residual + 25 nested. Neither belongs to a sector: the tail spans all
    // of them and the remainder belongs to no company at all.
    expect(groups.find((g) => g.paintKey === 'unattributed')?.value).toBe(300)
    expect(groups.find((g) => g.paintKey === 'other_companies')?.value).toBe(50)
  })

  it('leaves the structural groups out when there is nothing for them to carry', () => {
    const groups = buildExposureGroups(
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
    expect(groups.map((g) => g.paintKey).sort()).toEqual(['Financials', 'Technology'])
  })

  it('only company leaves can be clicked through to a breakdown', () => {
    for (const tile of allTiles(buildExposureGroups(response()))) {
      const structural = tile.key.startsWith('tile:')
      expect(tile.companyKey === null).toBe(structural)
    }
  })

  it('drops a zero-valued company rather than seating it in the hover layer', () => {
    const groups = buildExposureGroups(
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
    expect(allTiles(groups).some((t) => t.key === 'ZERO')).toBe(false)
  })

  it('orders groups and their children deterministically', () => {
    // Thousands of constituents sit at effectively zero; without a total order the layout would
    // shuffle between requests and a company would appear and vanish.
    const data = response()
    const once = buildExposureGroups(data).map((g) => [g.key, g.children.map((c) => c.key)])
    const again = buildExposureGroups({
      ...data,
      companies: [...data.companies].reverse(),
    }).map((g) => [g.key, g.children.map((c) => c.key)])
    expect(again).toEqual(once)
  })

  it('draws nothing at all when no position could be priced', () => {
    // The `concentrationPct` rule: an unpriced book has an unknown shape, and a chart of
    // confident rectangles is a worse answer than no chart.
    expect(buildExposureGroups(response({ total_market_value_eur: 0 }))).toEqual([])
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
