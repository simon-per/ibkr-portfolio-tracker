// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { cleanup, render } from '@testing-library/react'
import { DataTable, type Column } from './DataTable'
import { positionColumns } from '../PositionsList'
import { watchlistColumns } from '../watchlistColumns'
import { fundColumns, lookthroughColumns } from '../LookThroughTab'
import { cryptoHoldingColumns } from '../cryptoColumns'
import { dividendColumns } from '../DividendsTab'
import { activityColumns } from '../ActivityTab'
import { drillDownColumns } from '../AllocationTab'
import { drawdownColumns } from '../AnalyticsTab'
import { projectionColumns } from '../ForecastTab'
import { forecastMonthColumns } from '../DividendForecastTrend'
import { EARNINGS_COLUMNS, metricsColumns } from '../FundamentalsTab'
import { peerColumns } from '../CompanyPeersCard'
import { screenerColumns } from '../CompanyScreenerCard'
import {
  BY_COUNTRY_COLUMNS, DIVIDEND_INCOME_COLUMNS, HOLDINGS_COLUMNS, REALIZED_COLUMNS,
} from '../TaxTab'

/**
 * The family test CLAUDE.md asks for: written against "every table that renders through
 * DataTable", not against any one of them.
 *
 * `DataTable.test.tsx` pins the primitive's behaviour with a synthetic fixture. This
 * pins the *conventions* the real column sets have to follow — the ones that are not
 * expressible in the type system and that a twelfth table would otherwise reinvent or,
 * more likely, skip.
 *
 * It cannot see a table that never registers here. That is a real limit, stated rather
 * than papered over; `noRawTables` below is the backstop that catches a component going
 * around the primitive entirely.
 */

/**
 * Stubs that match the real formatters' contracts, including their null handling —
 * a stub that throws on null tests the stub, not the column.
 */
const money = (n: number) => `€${n.toFixed(2)}`
const moneyOrDash = (value: number | null) => (value === null ? '-' : `€${value.toFixed(2)}`)

/**
 * Every module-level column set is exported and registered here (review item U1,
 * 2026-11-08). Until then only four were: the Dividends table carried two `delta`
 * columns, and the phone card draws only the first, so its forward yield never reached a
 * phone. The sets still built inline (the hidden Analytics tab's tables, the rebalance
 * plan) are covered by the backstops at the bottom of this file.
 */
/**
 * Each entry carries a row with every *nullable* field actually null — the shape the
 * API really produces for an unsynced security, and the one that used to take a whole
 * tab down when a cell assumed a value. Fields the type declares non-nullable are given
 * a value, because a missing key is a state the API cannot reach and asserting on it
 * would be a phantom failure.
 */
const SPARSE_POSITION = {
  security_id: 1,
  symbol: 'X',
  description: '',
  isin: '',
  exchange: null,
  quantity: 0,
  cost_basis_eur: 0,
  market_value_eur: 0,
  gain_loss_eur: 0,
  gain_loss_percent: 0,
  market_price: null,
  analyst_rating: null,
}

const SPARSE_WATCHLIST = {
  id: 1,
  yahoo_ticker: 'X',
  symbol: null,
  company_name: null,
  notes: null,
  target_price: null,
  current_price: null,
  currency: null,
  trailing_pe: null,
  forward_pe: null,
  peg_ratio: null,
  ev_to_ebitda: null,
  revenue_growth: null,
  earnings_growth: null,
  fwd_revenue_growth: null,
  fwd_eps_growth: null,
  profit_margins: null,
  market_cap: null,
  market_cap_base: null,
  analyst_target: null,
  analyst_rating: null,
  analyst_count: null,
  week52_high: null,
  week52_low: null,
  pct_from_52w_high: null,
  ma200: null,
  ma50: null,
  pct_from_ma200: null,
  rsi14: null,
  buy_score: null,
  data_currency: null,
}

const SPARSE_LOOKTHROUGH = {
  company_key: 'XX0000000000',
  key_type: 'unidentified',
  name: 'Unknown Holding',
  value_eur: 0,
  pct_of_portfolio: 0,
  direct_value_eur: 0,
  via_funds_value_eur: 0,
  isins: [],
  listings: [],
  via_funds: [],
  partially_resolved: false,
  identity_conflicts: [],
}

// An unpriced coin with nothing reported: the shape CoinStats produces for a token it
// cannot identify, and every figure but the quantity is null.
const SPARSE_CRYPTO_HOLDING = {
  coin_id: 'x',
  symbol: null,
  name: null,
  rank: null,
  status: 'unpriced',
  quantity: 0,
  price: null,
  price_source: null,
  value: null,
  weight_pct: null,
  change_today_pct: null,
}

const SPARSE_DIVIDEND = {
  security_id: 1, symbol: 'X', exchange: null, description: '', payouts: 0, gross_eur: 0,
  withholding_eur: 0, net_eur: 0, forecast_payouts: 0, forecast_net_eur: 0,
  forward_yield_pct: null, per_share_growth_pct: null, yield_on_cost_pct: null,
  share_pct: null, next_pay_date: null, source: null, forecast_basis: null,
  forecast_samples: null, forecast_cadence_days: null, forecast_lag_days: null,
  forecast_lag_samples: null, forecast_method: null, forecast_withholding_pct: null,
  forecast_withholding_source: null,
}

// A cash flow: no symbol, no security, no quantity or price.
const SPARSE_ACTIVITY = {
  date: '2026-01-02', kind: 'cash', subtype: 'DEPOSITWITHDRAW', symbol: null,
  security_id: null, description: '', quantity: null, price: null, currency: null,
  amount_base: null, realized_pnl_base: null, counts_as_money_in: null, source: null,
  ib_key: null, is_sale: null, is_cancellation: null,
}

const SPARSE_ALLOCATION_POSITION = {
  symbol: 'X', security_id: null, description: '', weight: 0, market_value_eur: 0,
  is_etf_contribution: false,
}

// A fall still open, with no benchmark beside it.
const SPARSE_DRAWDOWN = {
  peakDate: '2026-01-02', troughDate: '2026-02-02', recoveredDate: null, depthPct: 0,
  daysToTrough: 0, daysToRecover: null, benchmarkPct: null,
}

// Today's value unknown: every figure of the projection is.
const SPARSE_PROJECTION = { year: 1, value: null, moneyIn: null, gains: null }

const SPARSE_PEER = {
  cik: 1, security_id: null, symbol: 'X', name: '', isin: null, held: false, currency: null,
  key_stats: {
    label: null, period_end: null, revenue: null, revenue_yoy: null, revenue_cagr_5y: null,
    net_income: null, eps_diluted: null, eps_yoy: null, eps_cagr_5y: null, fcf: null,
    fcf_per_share: null, fcf_yoy: null, gross_margin: null, operating_margin: null,
    net_margin: null, fcf_margin: null, diluted_shares: null, shares_yoy: null,
    dividends_per_share: null, payout_ratio: null, pe_ratio: null, market_cap: null,
    net_cash: null, enterprise_value: null, ev_ebit: null, ps_ratio: null,
  },
}

const SPARSE_SCREENER = {
  cik: 1, security_id: null, symbol: 'X', name: '', isin: null, held: false, currency: null,
  industry: null, period_end: null, market_cap: null, revenue_yoy: null, revenue_cagr_5y: null,
  eps_yoy: null, eps_cagr_5y: null, gross_margin: null, operating_margin: null, fcf_margin: null,
  roe: null, net_cash: null, pe_ratio: null, pe_median_5y: null, pe_vs_median: null,
  pfcf_ratio: null, pfcf_median_5y: null, pfcf_vs_median: null, ps_ratio: null,
  ps_median_5y: null, ps_vs_median: null, dividend_yield: null, dps_cagr_5y: null,
  raise_years: null, payout_ratio: null,
}

const SPARSE_FUNDAMENTALS = {
  security_id: 1, symbol: 'X', description: '', exchange: null, currency: 'USD',
  quote_type: null, trailing_pe: null, forward_pe: null, peg_ratio: null,
  price_to_sales: null, price_to_book: null, revenue_growth: null, earnings_growth: null,
  fwd_revenue_growth: null, fwd_eps_growth: null, profit_margins: null,
  gross_margins: null, operating_margins: null, market_cap: null, market_cap_base: null,
  number_of_analysts: null, target_mean_price: null, target_high_price: null,
  target_low_price: null, data_currency: null, last_updated: null,
}

const SPARSE_EARNINGS = {
  security_id: 1, symbol: 'X', symbols: [], description: '',
  earnings_date: '2026-01-02T00:00:00Z', eps_estimate: null, reported_eps: null,
  surprise_percent: null, beat_or_miss: null,
}

const SPARSE_FUND_COVERAGE = {
  symbol: null, fund_isin: 'XX0000000000', market_value_eur: 0, status: 'no_basket',
  reason: null, basket_as_of: null, stale: false, constituents: 0,
  equity_weight_pct: null, residual_eur: 0, asset_class_available: false, source: null,
  proxy_for_symbol: null, unweighted_constituents: 0,
}

const SPARSE_TAX_DIVIDEND = {
  symbol: null, description: null, isin: null, pay_date: '2026-01-02', gross: 0,
  withholding: null, net: 0, source: 'yfinance_estimate',
}

const SPARSE_TAX_COUNTRY = {
  country: 'XX', positions: 0, gross: 0, withholding: null, rate_pct: null,
  residual_pct: null, creditable: null, excess: null, net: 0, payments: 0,
  estimated_payments: 0,
}

const SPARSE_TAX_REALIZED = {
  symbol: null, security_id: null, isin: null, trade_date: '2026-01-02', quantity: null,
  proceeds: 0, cost_basis: 0, gain_loss: 0,
}

const SPARSE_TAX_HOLDING = {
  symbol: null, security_id: 1, isin: null, quantity: null, market_value: 0, cost_basis: 0,
}

const FAMILY: Array<[string, Column<never, string>[], unknown]> = [
  [
    'Positions',
    // An EMPTY yield map on purpose: a security with no payments and no projection is
    // absent from the breakdown rather than present-with-null, and that absence is the
    // sparse case the null-safety assertion below has to cover. Passing a populated map
    // would exercise the easy path only.
    positionColumns({
      formatCurrency: money, totalMarketValue: 1000, yieldOnCost: new Map(),
    }) as never,
    SPARSE_POSITION,
  ],
  ['Watchlist', watchlistColumns({ formatCurrency: moneyOrDash, currencySymbol: '€' }) as never, SPARSE_WATCHLIST],
  [
    'Look-through',
    lookthroughColumns({ formatCurrency: money }) as never,
    // The sparse case that actually occurs: a company reached ONLY through funds has no
    // directly-held listing, and one reached only directly has no contributing funds. Both
    // arrays empty at once is the shape a cell must not index into blindly.
    SPARSE_LOOKTHROUGH,
  ],
  [
    'Crypto holdings',
    cryptoHoldingColumns({ formatCurrency: money, formatPrice: money }) as never,
    SPARSE_CRYPTO_HOLDING,
  ],
  [
    'Dividends by position',
    dividendColumns({ formatCurrency: money, duplicated: new Set(), showForecast: true }) as never,
    SPARSE_DIVIDEND,
  ],
  ['Activity', activityColumns({ baseCurrency: 'EUR' }) as never, SPARSE_ACTIVITY],
  ['Allocation drill-down', drillDownColumns({ formatCurrency: money }) as never, SPARSE_ALLOCATION_POSITION],
  ['Drawdowns', drawdownColumns(null) as never, SPARSE_DRAWDOWN],
  ['Forecast projection', projectionColumns(money) as never, SPARSE_PROJECTION],
  ['Fundamentals metrics', metricsColumns('€') as never, SPARSE_FUNDAMENTALS],
  ['Earnings history', EARNINGS_COLUMNS as never, SPARSE_EARNINGS],
  ['Company peers', peerColumns() as never, SPARSE_PEER],
  ['Company screener', screenerColumns() as never, SPARSE_SCREENER],
  ['Look-through funds', fundColumns({ formatCurrency: money }) as never, SPARSE_FUND_COVERAGE],
  ['Tax dividend income', DIVIDEND_INCOME_COLUMNS(money) as never, SPARSE_TAX_DIVIDEND],
  ['Tax by country', BY_COUNTRY_COLUMNS(money) as never, SPARSE_TAX_COUNTRY],
  ['Tax realized gains', REALIZED_COLUMNS(money) as never, SPARSE_TAX_REALIZED],
  ['Tax holdings', HOLDINGS_COLUMNS(money) as never, SPARSE_TAX_HOLDING],
  [
    'Forecast by month',
    forecastMonthColumns(money) as never,
    { month: '2026-01', change: 0, buys: 0, raises: 0, other: 0 },
  ],
]

describe.each(FAMILY)('%s columns follow the shared conventions', (_name, columns, sparseRow) => {
  it('has exactly one title column — the card needs a heading and only one', () => {
    expect(columns.filter(c => c.mobile === 'title')).toHaveLength(1)
  })

  it('has at most one value and at most one delta', () => {
    expect(columns.filter(c => c.mobile === 'value').length).toBeLessThanOrEqual(1)
    expect(columns.filter(c => c.mobile === 'delta').length).toBeLessThanOrEqual(1)
  })

  it('has unique keys, which the footer spans and React both rely on', () => {
    const keys = columns.map(c => c.key)
    expect(new Set(keys).size).toBe(keys.length)
  })

  it('gives every sortable column a plain-text label', () => {
    // The phone's sort control is a native <select>, and an <option> cannot hold JSX.
    for (const col of columns.filter(c => c.sortKey)) {
      const label = col.shortHeader ?? (typeof col.header === 'string' ? col.header : null)
      expect(label, `${col.key} is sortable but has no string label`).toBeTruthy()
    }
  })

  it('never hides a column from both renderings', () => {
    // A column visible nowhere is a mistake, not a configuration.
    for (const col of columns) {
      expect(
        col.mobile === 'hide' && col.desktop === 'hide',
        `${col.key} is hidden in both modes`,
      ).toBe(false)
    }
  })

  it('carries the desktop avatar into the phone card title, once (#70)', () => {
    // At 390px no table but Positions showed a logo: the desktop cell drew one and the
    // card's title branch did not. The title column's `avatar` is drawn by DataTable.
    const row = sparseRow as never
    const title = columns.find(c => c.mobile === 'title')!
    const avatars = (root: Element) => root.querySelectorAll('span[aria-hidden="true"].rounded-full').length
    const desktop = render(<>{title.cell(row, 'table')}</>).container
    const cards = render(
      <DataTable rows={[row]} columns={columns} getRowKey={() => 1} label="t" mode="cards" />,
    ).container
    const cardTitle = cards.querySelector('li .truncate')!
    expect(avatars(cardTitle)).toBe(avatars(desktop))
    expect(avatars(cardTitle)).toBeLessThanOrEqual(1)
    cleanup()
  })

  it('renders every cell without throwing on an all-null row', () => {
    // An unsynced watchlist entry really is almost entirely null, and a cell that
    // assumes a value takes the whole tab down rather than showing one blank column.
    const sparse = sparseRow as never
    for (const col of columns) {
      expect(() => col.cell(sparse, 'table'), `${col.key} (table)`).not.toThrow()
      expect(() => col.cell(sparse, 'cards'), `${col.key} (cards)`).not.toThrow()
    }
  })
})

describe('no component renders a table outside the primitive', () => {
  it('leaves raw <table> markup only in ui/', async () => {
    // The backstop for column sets this file cannot import. A new `<table>` under
    // components/ means a surface that will be a spreadsheet on a phone again — the
    // exact regression this whole body of work removed.
    const sources = import.meta.glob('../*.tsx', { query: '?raw', import: 'default', eager: true })
    const offenders = Object.entries(sources)
      .filter(([path]) => !path.endsWith('.test.tsx'))
      .filter(([, src]) => /<table[\s>]/.test(src as string))
      .map(([path]) => path)

    // Three deliberate exceptions, each argued in the file that keeps it. Adding to
    // this list should feel like it needs a reason, because it does.
    expect(offenders.sort(), 'render these through ui/DataTable instead').toEqual([
      // A currency breakdown has no identity column to promote and no headline figure
      // to lead with, and the comparison *between* rows is the point.
      '../CurrencyExposureCard.tsx',
      // Both of these are 12-month x N-year matrices. Cards would buy one honest
      // scroll for a much longer page plus a second rendering of the same data.
      '../DividendSummary.tsx',
      '../MonthlyReturnsHeatmap.tsx',
    ])
  })

  it('gives every title column that draws an avatar on desktop an `avatar` for the card', () => {
    // The backstop for the module-local column sets (Activity, Dividends, Tax, Fundamentals,
    // Allocation, Rebalance): a module with a card title that draws a logo anywhere declares
    // the title's `avatar`, so the card cannot be the one rendering left without it.
    const sources = import.meta.glob('../*.tsx', { query: '?raw', import: 'default', eager: true })
    const offenders = Object.entries(sources)
      .filter(([path]) => !path.endsWith('.test.tsx'))
      .filter(([, src]) => /mobile: 'title'/.test(src as string))
      .filter(([, src]) => /<(TickerAvatar|ConstituentAvatar)\b/.test(src as string))
      .filter(([, src]) => !/\bavatar: /.test(src as string))
      .map(([path]) => path)
    expect(offenders).toEqual([])
  })

  it('has no consumer passing DataTable\'s test-only mode prop', () => {
    const sources = import.meta.glob('../*.tsx', { query: '?raw', import: 'default', eager: true })
    const offenders = Object.entries(sources)
      .filter(([path]) => !path.endsWith('.test.tsx'))
      .filter(([, src]) => /\bmode=["{](?:table|cards)/.test(src as string))
      .map(([path]) => path)
    expect(offenders).toEqual([])
  })
})
