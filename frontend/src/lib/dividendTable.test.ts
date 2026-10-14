import { describe, expect, it } from 'vitest'
import type { DividendSecurityRow } from './api'
import { DEFAULT_DIVIDEND_SORT, initialDirection, sortDividendRows } from './dividendTable'

function row(security_id: number, symbol: string, extra: Partial<DividendSecurityRow> = {}): DividendSecurityRow {
  return {
    security_id, symbol, exchange: null, description: symbol,
    payouts: 1, gross_eur: 0, withholding_eur: 0, net_eur: 0,
    forecast_payouts: 0, forecast_net_eur: 0, trailing_yield_pct: null,
    forward_yield_pct: null, per_share_growth_pct: null, trailing_yield_partial: false,
    days_held_in_ttm: 365, yield_on_cost_pct: null, share_pct: null, next_pay_date: null,
    source: 'ibkr', forecast_basis: null, forecast_samples: null, forecast_cadence_days: null,
    forecast_lag_days: null, forecast_lag_samples: null, forecast_method: null,
    forecast_withholding_pct: null, forecast_withholding_source: null,
    ...extra,
  }
}

const symbols = (rows: DividendSecurityRow[]) => rows.map((r) => r.symbol)

describe('sortDividendRows', () => {
  const rows = [
    row(1, 'AAA', { per_share_growth_pct: 4, net_eur: 10, forward_yield_pct: 1.2 }),
    row(2, 'BBB', { per_share_growth_pct: null, net_eur: 30, forward_yield_pct: null }),
    row(3, 'CCC', { per_share_growth_pct: -8, net_eur: 20, forward_yield_pct: 3.4 }),
  ]

  it('sorts a figure either way with the unknown last both times', () => {
    expect(symbols(sortDividendRows(rows, 'growth', 'desc', true))).toEqual(['AAA', 'CCC', 'BBB'])
    expect(symbols(sortDividendRows(rows, 'growth', 'asc', true))).toEqual(['CCC', 'AAA', 'BBB'])
    expect(symbols(sortDividendRows(rows, 'fwdYield', 'asc', true))).toEqual(['AAA', 'CCC', 'BBB'])
  })

  it('sorts net and leaves the input alone', () => {
    expect(symbols(sortDividendRows(rows, 'net', 'desc', true))).toEqual(['BBB', 'CCC', 'AAA'])
    expect(symbols(rows)).toEqual(['AAA', 'BBB', 'CCC'])
  })

  it('sorts the forecast as the cell shows it: a dash when the toggle is off', () => {
    const forecast = [
      row(1, 'AAA', { forecast_net_eur: 5 }),
      row(2, 'BBB', { forecast_net_eur: 0 }),
      row(3, 'CCC', { forecast_net_eur: 9 }),
    ]
    expect(symbols(sortDividendRows(forecast, 'forecast', 'asc', true))).toEqual(['AAA', 'CCC', 'BBB'])
    // All unknown with projections hidden: the tie-break decides, not a hidden figure.
    expect(symbols(sortDividendRows(forecast, 'forecast', 'desc', false))).toEqual(['AAA', 'BBB', 'CCC'])
  })

  it('breaks ties on the symbol, then the security', () => {
    const tied = [row(2, 'ASML', { net_eur: 5 }), row(1, 'ASML', { net_eur: 5 }), row(3, 'AAA', { net_eur: 5 })]
    expect(sortDividendRows(tied, 'net', 'desc', true).map((r) => r.security_id)).toEqual([3, 1, 2])
  })

  it('defaults to the server order, received plus projected biggest first', () => {
    const server = [
      row(1, 'AAA', { net_eur: 0, forecast_net_eur: 50, share_pct: 50 }),
      row(2, 'BBB', { net_eur: 30, forecast_net_eur: 0, share_pct: 30 }),
      row(3, 'CCC', { net_eur: 20, forecast_net_eur: 0, share_pct: 20 }),
    ]
    const { column, direction } = DEFAULT_DIVIDEND_SORT
    expect(symbols(sortDividendRows(server, column, direction, true))).toEqual(['AAA', 'BBB', 'CCC'])
  })

  it('starts names and dates ascending and figures descending', () => {
    expect(initialDirection('symbol')).toBe('asc')
    expect(initialDirection('next')).toBe('asc')
    expect(initialDirection('yoc')).toBe('desc')
  })
})
