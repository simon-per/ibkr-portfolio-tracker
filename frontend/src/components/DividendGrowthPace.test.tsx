// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { DividendTtmPace } from '@/lib/api'
import { DividendGrowthPace } from './DividendGrowthPace'

afterEach(cleanup)

function pace(over: Partial<DividendTtmPace> = {}): DividendTtmPace {
  return {
    monthly_pct: 12.25, annualized_pct: 300, from_month: '2025-12', to_month: '2026-06',
    from_eur: 120, to_eur: 240, months: 6, short_history: false,
    includes_forecast: false, crosses_era: false,
    ...over,
  }
}

describe('the dividend growth pace strip', () => {
  it('shows the rate in both units, and the windows it was measured between', () => {
    render(<DividendGrowthPace pace={pace()} />)
    const strip = screen.getByLabelText('Dividend growth pace')

    // Rendered by DeltaChip at the app's one decimal place; the wire carries two,
    // which is what the compounding identity is checked against server-side.
    expect(strip.textContent).toContain('+12.3%')
    expect(strip.textContent).toContain('per month')
    expect(strip.textContent).toContain('+300%')
    expect(strip.textContent).toContain('a year')
    // The two totals it compounds between, on the surface — they are what makes
    // the rate checkable against the chart below rather than merely asserted.
    expect(strip.textContent).toContain('Jan 25 – Dec 25')
    expect(strip.textContent).toContain('Jul 25 – Jun 26')
    expect(strip.textContent).toContain('120.00')
    expect(strip.textContent).toContain('240.00')
  })

  it('does not call a measured rate an estimate', () => {
    render(<DividendGrowthPace pace={pace()} />)
    expect(screen.getByLabelText('Dividend growth pace').textContent).not.toContain('est.')
  })

  it('marks a rate resting on projected windows, and says what that costs', () => {
    // The whole point of the projected basis: the forward rate is as much a
    // statement about the inferred payout schedule as about the portfolio, and
    // that has to be legible without hovering anything.
    render(<DividendGrowthPace pace={pace({ includes_forecast: true })} />)
    const strip = screen.getByLabelText('Dividend growth pace')

    expect(strip.textContent).toContain('est.')
    expect(strip.textContent).toContain('keeps paying at its current per-share rate')
  })

  it('states the span when it is shorter than the six months claimed', () => {
    // A rate labelled "per month" that rests on three of them is a different
    // claim, so the span travels with the figure instead of sitting in a title.
    render(<DividendGrowthPace pace={pace({ months: 3, short_history: true })} />)
    expect(screen.getByLabelText('Dividend growth pace').textContent).toContain('over 3 months of the rolling series, not 6')
  })

  it('always says this is not per-share dividend growth', () => {
    // As visible text. The distinction between a portfolio earning more because
    // it bought more and a company raising its dividend is the one a reader is
    // most likely to get wrong, and a caveat behind a hover does not exist.
    render(<DividendGrowthPace pace={pace()} />)
    expect(screen.getByLabelText('Dividend growth pace').textContent).toContain('not a measure of dividend increases per share')
  })

  it('renders nothing rather than a dash when the server sends no pace', () => {
    // Absent, not zeroed: "0.0% per month" on a book with too little history
    // reads as "flat", which is an answer it has not earned.
    const { container } = render(<DividendGrowthPace pace={null} />)
    expect(container.innerHTML).toBe('')
  })
})
