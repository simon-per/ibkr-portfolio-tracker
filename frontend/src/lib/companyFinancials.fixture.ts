import type {
  CompanyFinancialsCompany,
  CompanyFinancialsResponse,
  CompanyFinancialsSeries,
  CompanyFinancialsSeriesSet,
} from './api'

/**
 * Test fixtures for the Company tab, shaped like a January fiscal year (NVDA-like): fiscal
 * years end in late January, so FY2025 ends 2025-01-26. Annual, four periods, with a loss
 * year and a gap. Tests only — nothing in the app imports this.
 */
const PERIODS = [
  { period_end: '2022-01-30', label: 'FY2022' },
  { period_end: '2023-01-29', label: 'FY2023' },
  { period_end: '2024-01-28', label: 'FY2024' },
  { period_end: '2025-01-26', label: 'FY2025' },
]

export function fixtureSeries(
  values: (number | null)[],
  cagr: number | null = null,
  yoy: number | null = null,
): CompanyFinancialsSeries {
  return {
    // Each point's YoY: the last one carries the series' own, the earlier ones none.
    points: values.map((value, i) => ({ ...PERIODS[i], value, yoy: i === values.length - 1 ? yoy : null })),
    cagr,
    yoy,
  }
}

export const FIXTURE_COMPANIES: CompanyFinancialsCompany[] = [
  {
    security_id: 11, symbol: 'NVDA', name: 'NVIDIA CORP', cik: '0001045810', currency: 'USD',
    first_period_end: '2009-01-25', last_period_end: '2025-01-26', last_fetched_at: '2026-10-24T06:00:00',
  },
  {
    security_id: 22, symbol: 'AMZN', name: 'AMAZON COM INC', cik: '0001018724', currency: 'USD',
    first_period_end: '2009-12-31', last_period_end: '2025-06-30', last_fetched_at: '2026-10-24T06:00:00',
  },
]

export function fixtureFinancials(
  over: Partial<Omit<CompanyFinancialsResponse, 'series'>> & {
    series?: Partial<CompanyFinancialsSeriesSet>
  } = {},
): CompanyFinancialsResponse {
  const { series, ...rest } = over
  return {
    security_id: 11,
    symbol: 'NVDA',
    name: 'NVIDIA CORP',
    currency: 'USD',
    period: 'annual',
    range: '10y',
    as_of: '2026-10-24',
    warnings: [],
    series: {
      revenue: fixtureSeries([26_914e6, 26_974e6, 60_922e6, 130_497e6], 0.693, 1.142),
      revenue_per_share: fixtureSeries([1.06, 1.08, 2.44, 5.26], 0.705, 1.156),
      net_income: fixtureSeries([9_752e6, -4_368e6, 29_760e6, 72_880e6], null, 1.449),
      eps_diluted: fixtureSeries([0.39, 0.17, 1.19, 2.94], 0.961, 1.471),
      fcf: fixtureSeries([8_132e6, 3_808e6, null, 60_853e6], 0.956, null),
      fcf_after_sbc: fixtureSeries([6_128e6, 1_098e6, null, 56_116e6], 1.093, null),
      fcf_per_share: fixtureSeries([0.32, 0.15, null, 2.45], 0.971, null),
      fcf_after_sbc_per_share: fixtureSeries([0.24, 0.04, null, 2.26], 1.111, null),
      diluted_shares: fixtureSeries([25_350e6, 25_070e6, 24_940e6, 24_804e6], -0.007, -0.005),
      operating_margin: fixtureSeries([0.372, 0.157, 0.541, 0.624]),
      net_margin: fixtureSeries([0.362, 0.162, 0.488, 0.558]),
      fcf_margin: fixtureSeries([0.302, 0.141, null, 0.466]),
      dividends_per_share: fixtureSeries([0.016, 0.016, 0.016, 0.034], 0.286, 1.125),
      ...series,
    },
    ...rest,
  }
}

/** Every series empty: a company the server knows but has no figures for. */
export function emptySeriesSet(): CompanyFinancialsSeriesSet {
  const empty = (): CompanyFinancialsSeries => ({ points: [], cagr: null, yoy: null })
  return {
    revenue: empty(), revenue_per_share: empty(), net_income: empty(), eps_diluted: empty(),
    fcf: empty(), fcf_after_sbc: empty(), fcf_per_share: empty(), fcf_after_sbc_per_share: empty(),
    diluted_shares: empty(),
    operating_margin: empty(), net_margin: empty(), fcf_margin: empty(),
    dividends_per_share: empty(),
  }
}
