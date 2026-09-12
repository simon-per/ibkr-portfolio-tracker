// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { PortfolioSummaryCards } from './PortfolioSummaryCards'
import type { PortfolioSummary } from '@/lib/api'

/**
 * Market Value is the most prominent number in the app, and it is a **sum over holdings
 * the backend could price**. A holding it could not price is dropped from the total while
 * its cost stays in Cost Basis, so the headline understates and both gain figures with it.
 *
 * That is the SBI shape exactly: deleting one security's poisoned prices took 446.93 CHF
 * off this number, and the only thing that said so was a sync warning nobody has to read.
 */

afterEach(cleanup)

const base: PortfolioSummary = {
  total_cost_basis_eur: 48561.72,
  total_market_value_eur: 65024.51,
  total_gain_loss_eur: 16462.79,
  total_gain_loss_percent: 33.9,
  num_positions: 36,
  total_realized_gain_loss_eur: -12.5,
  total_realized_proceeds_eur: 400,
  total_realized_cost_basis_eur: 412.5,
  num_closed_positions: 4,
  base_currency: 'CHF',
}

const renderWith = (over: Partial<PortfolioSummary> = {}) =>
  render(<PortfolioSummaryCards summary={{ ...base, ...over }} />)

describe('the incomplete-total notice', () => {
  it('stays hidden when every holding was priced', () => {
    renderWith({ unpriced_holdings: 0 })
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('stays hidden when the backend does not report the field', () => {
    // Older than 2026-08-05. Absent must read as complete, or every deployment before
    // this change would carry a permanent warning on its headline figure.
    renderWith()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('says the headline is incomplete, and why, when a holding cannot be priced', () => {
    renderWith({ unpriced_holdings: 2 })
    const alert = screen.getByRole('alert')
    expect(alert.textContent).toMatch(/Market Value is incomplete/)
    expect(alert.textContent).toMatch(/2 holdings have no usable price/)
    // The asymmetry is the point: cost is whole, value is not.
    expect(alert.textContent).toMatch(/cost still counts toward Cost Basis/)
  })

  it('uses the singular for one holding', () => {
    renderWith({ unpriced_holdings: 1 })
    expect(screen.getByRole('alert').textContent).toMatch(/1 holding has no usable price/)
  })

  it('still renders the figures rather than hiding them', () => {
    // The total is understated, not unknown — blanking it would lose more than it saves,
    // and Cost Basis and Positions are unaffected.
    renderWith({ unpriced_holdings: 2 })
    expect(screen.getByText('36')).toBeTruthy()
    expect(screen.getByRole('alert')).toBeTruthy()
  })
})

describe('the period-change chip', () => {
  it('shows the change and its range when one is known', () => {
    render(<PortfolioSummaryCards summary={base} periodChangePct={4.2} periodLabel="1Y" />)
    expect(screen.getByText(/over 1Y/)).toBeTruthy()
  })

  it('renders no chip at all for an unknown change, rather than a 0% one', () => {
    // `periodChange` publishes null when nothing was held at the range start — a range
    // that begins before inception — and the card must not turn that back into a figure.
    render(<PortfolioSummaryCards summary={base} periodChangePct={null} periodLabel="2Y" />)
    expect(screen.queryByText(/over 2Y/)).toBeNull()
    expect(screen.queryByText(/\+0\.0+%/)).toBeNull()
  })
})
