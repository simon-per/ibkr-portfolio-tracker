// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { PerformanceInsights } from './PerformanceInsights'
import type { DividendUpcomingPayment, PerformanceAttributionResponse, Position } from '@/lib/api'

afterEach(cleanup)

/** Made-up figures throughout. */
function attribution(rows: Array<[number, string, number]>): PerformanceAttributionResponse {
  return {
    start_date: '2026-01-01', end_date: '2026-05-10', total_pnl_eur: 0,
    attributions: rows.map(([security_id, symbol, pnl]) => ({
      security_id, symbol, description: `${symbol} Corp`, start_market_value_eur: 100,
      end_market_value_eur: 100, new_investment_eur: 0, value_change_eur: pnl,
      pnl_contribution_eur: pnl, contribution_percent: 0, weight_percent: 0,
    })),
  }
}

function pos(security_id: number, symbol: string, value: number): Position {
  return {
    security_id, symbol, description: `${symbol} Corp`, isin: `X${security_id}`, currency: 'USD',
    exchange: 'NYSE', quantity: 1, cost_basis_eur: 50, market_value_eur: value, market_price: 1,
    gain_loss_eur: 0, gain_loss_percent: 0, taxlots: [], analyst_rating: null,
  }
}

const PAYMENT: DividendUpcomingPayment = {
  date: '2026-05-20', ex_date: null, security_id: 2, symbol: 'BBB', net_eur: 12.5,
  basis: 'gross_estimate', pay_date_source: 'measured_lag', amount_source: null, pending: false,
}

const tile = (label: string) => screen.getByText(label).closest('div')!.parentElement!

describe('PerformanceInsights', () => {
  it('shows the movers, the largest holding and the next dividend', () => {
    render(
      <PerformanceInsights
        attribution={attribution([[1, 'AAA', 300], [2, 'BBB', -40], [3, 'CCC', 20]])}
        positions={[pos(1, 'AAA', 750), pos(2, 'BBB', 250)]}
        cash={{ amount: 0, cash_source: 'derived' }}
        upcoming={[PAYMENT]}
        range="1Y"
        today="2026-05-10"
      />,
    )
    expect(tile('Top mover').textContent).toContain('AAA')
    expect(tile('Top mover').textContent).toContain('+€300.00')
    expect(tile('Biggest drag').textContent).toContain('BBB')
    expect(tile('Largest holding').textContent).toContain('75.0%')
    expect(tile('Next dividend').textContent).toContain('est.')
    expect(tile('Next dividend').textContent).toContain('May 20')
  })

  it('dashes what has not loaded and says None for what does not exist', () => {
    render(
      <PerformanceInsights
        attribution={attribution([[1, 'AAA', 300]])}
        positions={undefined}
        cash={{ amount: 0, cash_source: 'derived' }}
        upcoming={[]}
        range="1Y"
        today="2026-05-10"
      />,
    )
    expect(tile('Biggest drag').textContent).toContain('None')
    expect(tile('Largest holding').textContent).toContain('—')
    expect(tile('Next dividend').textContent).toContain('None')
  })

  it('renders nothing for an empty book', () => {
    const { container } = render(
      <PerformanceInsights attribution={undefined} positions={[]} cash={undefined} upcoming={undefined} range="1Y" />,
    )
    expect(container.innerHTML).toBe('')
  })

  it('dashes the largest holding until the summary (and its cash) arrives', () => {
    render(
      <PerformanceInsights
        attribution={attribution([[1, 'AAA', 300]])}
        positions={[pos(1, 'AAA', 750), pos(2, 'BBB', 250)]}
        cash={undefined}
        upcoming={[]}
        range="1Y"
        today="2026-05-10"
      />,
    )
    // Not "100.0%" of holdings alone, and not "None": the weight is not known yet.
    expect(tile('Largest holding').textContent).toContain('—')
    expect(tile('Largest holding').textContent).not.toContain('%')
  })
})
