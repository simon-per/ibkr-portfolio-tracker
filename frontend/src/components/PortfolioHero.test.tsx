// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { PortfolioHero } from './PortfolioHero'
import type { PortfolioSummary } from '@/lib/api'
import type { PeriodChange } from '@/lib/portfolioKpis'

/**
 * The headline is a **sum over holdings the backend could price**. A holding it could not
 * price is dropped from the total while its cost stays in cost basis, so the headline
 * understates and both gains with it — the SBI shape, which only a sync warning reported.
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

const period: PeriodChange = {
  startValue: 50000,
  currentValue: 65024.51,
  absoluteChange: 15024.51,
  percentageChange: 30.05,
  startDate: '2025-10-16',
  endDate: '2026-10-16',
  periodGain: 4200,
  periodGainPercent: 9.1,
}

const renderWith = (over: Partial<PortfolioSummary> = {}, p: PeriodChange | null = period) =>
  render(
    <PortfolioHero summary={{ ...base, ...over }} period={p} range="1Y" controls={null}>
      <div />
    </PortfolioHero>,
  )

describe('the incomplete-total notice', () => {
  it('stays hidden when every holding was priced', () => {
    renderWith({ unpriced_holdings: 0 })
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('stays hidden when the backend does not report the field', () => {
    renderWith()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('says the headline is incomplete when a holding cannot be priced', () => {
    renderWith({ unpriced_holdings: 2 })
    const alert = screen.getByRole('alert')
    expect(alert.textContent).toMatch(/Market value is incomplete/)
    expect(alert.textContent).toMatch(/2 holdings have no usable price/)
  })

  it('uses the singular for one holding', () => {
    renderWith({ unpriced_holdings: 1 })
    expect(screen.getByRole('alert').textContent).toMatch(/1 holding has no usable price/)
  })

  it('still renders the figures rather than hiding them', () => {
    renderWith({ unpriced_holdings: 3 })
    expect(screen.getByText('36')).toBeTruthy()
    expect(screen.getByRole('alert')).toBeTruthy()
  })
})

describe('the realized gain', () => {
  it('carries no tag when every sale was priced or the backend does not say', () => {
    renderWith({ unpriced_disposals: 0 })
    expect(screen.queryByText(/unpriced/)).toBeNull()
    cleanup()
    renderWith()
    expect(screen.queryByText(/unpriced/)).toBeNull()
  })

  it('tags the figure with the sales it left out', () => {
    renderWith({ unpriced_disposals: 2 })
    expect(screen.getByText('2 sales unpriced')).toBeTruthy()
    cleanup()
    renderWith({ unpriced_disposals: 1 })
    expect(screen.getByText('1 sale unpriced')).toBeTruthy()
  })
})

describe('cash a trade could not be converted into (GitHub #59)', () => {
  // What the backend sends: cash unknown and absent, never a 0.00 balance, plus the count.
  const unconverted: Partial<PortfolioSummary> = {
    cash_source: 'unknown', total_cash_eur: null, total_value_eur: null, cash_unconverted: 1,
  }

  it('shows no Cash figure and a holdings-only headline, and names the cause', () => {
    renderWith(unconverted)
    expect(screen.queryByText('Cash')).toBeNull()
    expect(screen.getByText('Market value')).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toMatch(/Cash is unknown — 1 trade has no FX rate/)
  })

  it('uses the plural, and stays silent when nothing was left out', () => {
    renderWith({ ...unconverted, cash_unconverted: 3 })
    expect(screen.getByRole('alert').textContent).toMatch(/3 trades have no FX rate/)
    cleanup()
    renderWith({ cash_source: 'derived', total_cash_eur: 100, total_value_eur: 65124.51, cash_unconverted: 0 })
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByText('Cash')).toBeTruthy()
  })
})

describe('the period gain', () => {
  it('shows the gain, its percentage and the range', () => {
    renderWith()
    expect(screen.getByText(/\+9\.10%/)).toBeTruthy()
    expect(screen.getByText('past year')).toBeTruthy()
  })

  it('renders nothing for an unknown period, rather than a 0% one', () => {
    renderWith({}, null)
    expect(screen.queryByText('past year')).toBeNull()
    expect(screen.queryByText(/\+0\.0+%/)).toBeNull()
  })

  it('leaves the percentage off when nothing was invested at the start', () => {
    renderWith({}, { ...period, periodGainPercent: null })
    expect(screen.getByText('past year')).toBeTruthy()
    expect(screen.queryByText(/\(\+/)).toBeNull()
  })
})

describe('a new account', () => {
  const empty: Partial<PortfolioSummary> = {
    total_cost_basis_eur: 0, total_market_value_eur: 0, total_gain_loss_eur: 0,
    // What the server sends for a gain on no cost (`share_pct`): null, not 0.
    total_gain_loss_percent: null, num_positions: 0, total_realized_gain_loss_eur: 0,
    total_realized_proceeds_eur: 0, total_realized_cost_basis_eur: 0, num_closed_positions: 0,
    cash_source: 'unknown',
  }

  it('says there is nothing yet instead of drawing zeros', () => {
    renderWith(empty, null)
    expect(screen.getByText('No portfolio data yet')).toBeTruthy()
    expect(screen.queryByText(/0\.00%/)).toBeNull()
    expect(screen.queryByText('Cost basis')).toBeNull()
  })

  it('still shows an account that holds only cash', () => {
    renderWith({ ...empty, cash_source: 'derived', total_cash_eur: 5000, total_value_eur: 5000 }, null)
    expect(screen.queryByText('No portfolio data yet')).toBeNull()
    expect(screen.getByText('Total value')).toBeTruthy()
  })

  it('leaves the unrealized percentage off a zero cost basis', () => {
    renderWith({ ...empty, cash_source: 'derived', total_cash_eur: 5000, total_value_eur: 5000 }, null)
    expect(screen.queryByText('+0.00%')).toBeNull()
  })
})

describe('an outage', () => {
  // `e2e/errors.mjs` counts "didn't respond" across the Performance tab; the hero is the
  // tab's largest surface, so it says it in the same words as every panel under it.
  it('says the server did not respond instead of showing a balance', () => {
    render(
      <PortfolioHero summary={undefined} isError period={null} range="1Y" controls={null}>
        <div />
      </PortfolioHero>,
    )
    expect(screen.getByText(/Couldn't load the portfolio — the server didn't respond/)).toBeTruthy()
    expect(screen.queryByText(/No portfolio data yet/)).toBeNull()
    expect(screen.queryByText(/0\.00/)).toBeNull()
  })
})
