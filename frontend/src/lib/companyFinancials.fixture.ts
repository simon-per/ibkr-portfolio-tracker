import type {
  CompanyFinancialsCompany,
  CompanyFinancialsKeyStats,
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
    // The fixture's range is 10Y, so its CAGR is the 10-year one; no 3- or 5-year history.
    cagrs: { y3: null, y5: null, y10: cagr },
  }
}

/** NVDA-like TTM key figures, with no gross margin filed and no buybacks. */
export const FIXTURE_KEY_STATS: CompanyFinancialsKeyStats = {
  label: 'Q4 FY2025', period_end: '2025-01-26',
  revenue: 130_497e6, revenue_yoy: 1.142, revenue_cagr_5y: 0.642,
  net_income: 72_880e6, eps_diluted: 2.94, eps_yoy: 1.471, eps_cagr_5y: 0.912,
  fcf: 60_853e6, fcf_per_share: 2.45, fcf_yoy: null,
  gross_margin: null, operating_margin: 0.624, net_margin: 0.558, fcf_margin: 0.466,
  diluted_shares: 24_804e6, shares_yoy: -0.005,
  dividends_per_share: 0.034, payout_ratio: 0.0116, pe_ratio: 48.27, market_cap: 3_509e9, net_cash: 34_800e6, enterprise_value: 3_474e9,
  ev_ebit: 42.6, ps_ratio: 26.9,
  pfcf_ratio: 57.6, dividend_yield: 0.00024, roe: 1.192,
  buybacks: null, dividends_paid: 834e6,
}

export const NVDA_CIK = 1045810
export const AMZN_CIK = 1018724
export const AAPL_CIK = 320193

export const FIXTURE_COMPANIES: CompanyFinancialsCompany[] = [
  {
    cik: AAPL_CIK, security_id: null, symbol: 'AAPL', name: 'Apple Inc', isin: 'US0378331005', held: false,
    currency: 'USD', first_period_end: '2009-06-27', last_period_end: '2025-06-28', last_fetched_at: '2026-10-24T06:00:00',
  },
  {
    cik: NVDA_CIK, security_id: 11, symbol: 'NVDA', name: 'NVIDIA CORP', isin: 'US67066G1040', held: true,
    currency: 'USD', first_period_end: '2009-01-25', last_period_end: '2025-01-26', last_fetched_at: '2026-10-24T06:00:00',
  },
  {
    cik: AMZN_CIK, security_id: 22, symbol: 'AMZN', name: 'AMAZON COM INC', isin: 'US0231351067', held: true,
    currency: 'USD', first_period_end: '2009-12-31', last_period_end: '2025-06-30', last_fetched_at: '2026-10-24T06:00:00',
  },
]

export function fixtureFinancials(
  over: Partial<Omit<CompanyFinancialsResponse, 'series'>> & {
    series?: Partial<CompanyFinancialsSeriesSet>
  } = {},
): CompanyFinancialsResponse {
  const { series, ...rest } = over
  return {
    cik: NVDA_CIK,
    security_id: 11,
    symbol: 'NVDA',
    name: 'NVIDIA CORP',
    isin: 'US67066G1040',
    held: true,
    currency: 'USD',
    period: 'annual',
    range: '10y',
    as_of: '2026-10-24',
    warnings: [],
    key_stats: FIXTURE_KEY_STATS,
    series: {
      revenue: fixtureSeries([26_914e6, 26_974e6, 60_922e6, 130_497e6], 0.693, 1.142),
      revenue_per_share: fixtureSeries([1.06, 1.08, 2.44, 5.26], 0.705, 1.156),
      operating_income: fixtureSeries([10_041e6, 4_224e6, 32_972e6, 81_453e6], 0.689, 1.470),
      net_income: fixtureSeries([9_752e6, -4_368e6, 29_760e6, 72_880e6], null, 1.449),
      eps_diluted: fixtureSeries([0.39, 0.17, 1.19, 2.94], 0.961, 1.471),
      fcf: fixtureSeries([8_132e6, 3_808e6, null, 60_853e6], 0.956, null),
      fcf_after_sbc: fixtureSeries([6_128e6, 1_098e6, null, 56_116e6], 1.093, null),
      fcf_per_share: fixtureSeries([0.32, 0.15, null, 2.45], 0.971, null),
      fcf_after_sbc_per_share: fixtureSeries([0.24, 0.04, null, 2.26], 1.111, null),
      diluted_shares: fixtureSeries([25_350e6, 25_070e6, 24_940e6, 24_804e6], -0.007, -0.005),
      gross_margin: fixtureSeries([0.649, 0.569, 0.727, 0.750]),
      operating_margin: fixtureSeries([0.372, 0.157, 0.541, 0.624]),
      net_margin: fixtureSeries([0.362, 0.162, 0.488, 0.558]),
      fcf_margin: fixtureSeries([0.302, 0.141, null, 0.466]),
      dividends_per_share: fixtureSeries([0.016, 0.016, 0.016, 0.034], 0.286, 1.125),
      buybacks: fixtureSeries([0, 10_039e6, 9_533e6, 33_706e6], null, 2.536),
      dividends_paid: fixtureSeries([399e6, 398e6, 395e6, 834e6], 0.279, 1.111),
      cash: fixtureSeries([1_990e6, 3_389e6, 7_280e6, 8_589e6], 0.628, 0.180),
      debt: fixtureSeries([10_946e6, 10_954e6, 9_709e6, 8_463e6], -0.082, -0.128),
      net_cash: fixtureSeries([10_200e6, 2_400e6, 16_300e6, 34_800e6]),
      pe_ratio: fixtureSeries([62.8, null, 51.4, 48.27]),
      ps_ratio: fixtureSeries([22.8, 13.9, 25.1, 26.9]),
      pfcf_ratio: fixtureSeries([75.3, 98.6, null, 57.6]),
      dividend_yield: fixtureSeries([0.0007, 0.0012, 0.0005, 0.00024]),
      roe: fixtureSeries([0.448, -0.165, 0.919, 1.192]),
      ...series,
    },
    ...rest,
  }
}

/** Every series empty: a company the server knows but has no figures for. */
export function emptySeriesSet(): CompanyFinancialsSeriesSet {
  const empty = (): CompanyFinancialsSeries => ({
    points: [], cagr: null, yoy: null, cagrs: { y3: null, y5: null, y10: null },
  })
  return {
    revenue: empty(), revenue_per_share: empty(), operating_income: empty(), net_income: empty(),
    eps_diluted: empty(),
    fcf: empty(), fcf_after_sbc: empty(), fcf_per_share: empty(), fcf_after_sbc_per_share: empty(),
    diluted_shares: empty(),
    gross_margin: empty(), operating_margin: empty(), net_margin: empty(), fcf_margin: empty(),
    dividends_per_share: empty(), buybacks: empty(), dividends_paid: empty(),
    cash: empty(), debt: empty(), net_cash: empty(), pe_ratio: empty(), ps_ratio: empty(), pfcf_ratio: empty(),
    dividend_yield: empty(), roe: empty(),
  }
}
