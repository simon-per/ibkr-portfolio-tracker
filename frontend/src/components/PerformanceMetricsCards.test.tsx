// @vitest-environment jsdom
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { PerformanceMetricsCards } from './PerformanceMetricsCards'

/**
 * This row had no test until effective holdings moved onto it.
 *
 * The footnote it moved into is the interesting part: `top5Weight` is a confident number
 * whenever positions exist, while `effectiveHoldings` is `null` when none of them is
 * priced (`herfindahlConcentration` refuses rather than counting a zero-valued row as a
 * small one). So the two can disagree about whether there is anything to say, and
 * concatenating them without checking prints `· null effective` under a `0.0%`.
 */

afterEach(cleanup)

type Metrics = Parameters<typeof PerformanceMetricsCards>[0]['metrics']

const base: NonNullable<Metrics> = {
  xirr: 12.4,
  xirrMethod: 'xirr',
  maxDrawdown: -12.5,
  sharpeRatio: 1.21,
  winRate: 61,
  profitablePositions: 22,
  totalPositions: 36,
  calmarRatio: 0.99,
  top5Weight: 63.4,
  effectiveHoldings: 12.3,
}

function renderWith(overrides: Partial<NonNullable<Metrics>> = {}) {
  return render(<PerformanceMetricsCards metrics={{ ...base, ...overrides }} />)
}

describe('PerformanceMetricsCards', () => {
  it('footnotes the effective holdings beside the concentration it qualifies', () => {
    renderWith()
    expect(screen.getByText('63.4%')).toBeTruthy()
    expect(screen.getByText('Concentration risk · 12.3 effective')).toBeTruthy()
  })

  it('drops the count rather than printing an absent one', () => {
    renderWith({ effectiveHoldings: null })
    expect(screen.getByText('Concentration risk')).toBeTruthy()
    expect(screen.queryByText(/effective/)).toBeNull()
  })

  it('does not report a green 0.0% concentration when nothing is priced', () => {
    // The one absence in this row that actively reassures: the tone ladder calls
    // anything under 50% good news, so an unpriced portfolio rendered a green "0.0%"
    // claiming the five largest holdings are none of the book — while the effective
    // count that would have qualified it correctly dropped out of the same footnote.
    renderWith({ top5Weight: null, effectiveHoldings: null })
    expect(screen.getByText('No priced positions')).toBeTruthy()
    expect(screen.queryByText('0.0%')).toBeNull()
    expect(screen.queryByText(/Concentration risk/)).toBeNull()
  })

  it('labels a short window a period return instead of annualizing it', () => {
    renderWith({ xirrMethod: 'simple_period' })
    expect(screen.getByText('Period Return')).toBeTruthy()
    expect(screen.queryByText('Annual Return (XIRR)')).toBeNull()
  })

  it('shows a dash, never a zero, for an unresolved return', () => {
    renderWith({ xirr: null, calmarRatio: null })
    expect(screen.queryByText('0.00%')).toBeNull()
    expect(screen.queryByText('0.00')).toBeNull()
  })

  it('says why Sharpe is absent instead of rendering a plausible 0.00', () => {
    // Reachable in one click before this: MTD in the first days of a month leaves 2
    // daily returns, and `sharpeRatio` returned 0 — drawn green, captioned
    // "Risk-adjusted return", beside a dashed Volatility and Sortino. A 0.00 Sharpe is
    // plausible, so nothing about it invited doubt.
    renderWith({ sharpeRatio: null })
    expect(screen.getByText('Not enough history in this range')).toBeTruthy()
    expect(screen.queryByText('0.00')).toBeNull()
  })

  // Not asserting "the absent Sharpe isn't green" here: other cards in this row are
  // legitimately green, so a container-wide query catches them instead. `KpiCard` refuses
  // to colour a `value={null}` regardless of the `tone` passed, and owns that test.

  it('renders nothing at all rather than an empty shell', () => {
    const { container } = render(<PerformanceMetricsCards metrics={null} />)
    expect(container.firstChild).toBeNull()
  })

  it('shows six loading placeholders, matching the grid it fills', () => {
    const { container } = render(<PerformanceMetricsCards metrics={null} isLoading />)
    expect(container.querySelectorAll('.animate-pulse')).toHaveLength(6)
  })
})

/**
 * Both of these read a figure computed from an incomplete valuation. The backend reports
 * its own completeness now; this row's job is to not present the number as a measurement
 * anyway.
 */
describe('an incomplete valuation is stated, not absorbed', () => {
  it('says so above the row when the return was measured against unpriced holdings', () => {
    renderWith({ xirrUnpriced: 2 })
    const alert = screen.getByRole('alert')
    expect(alert.textContent).toMatch(/2 holdings had no usable price/)
    // The caveat sits outside the cards, in the row's own flow: a qualifier you have to
    // open or hover something to reach does not exist on a touch device.
    expect(alert.textContent).toMatch(/Calmar/)
  })

  it('says nothing when the valuation was complete', () => {
    renderWith({ xirrUnpriced: 0 })
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('does not count an unpriced holding as a losing position', () => {
    // The count beside the ratio, not only in the alert: an unpriced holding is valued at
    // 0.00 and so carries a gain of -cost, which used to make it an ordinary-looking
    // loser. Three of them did exactly that for hours on 2026-08-07.
    renderWith({ winRate: 100, profitablePositions: 36, totalPositions: 36, unvaluedPositions: 3 })
    expect(screen.getByText('36 of 36 profitable · 3 unpriced, not judged')).toBeTruthy()
  })

  it('has no win rate at all when nothing could be valued', () => {
    // Not 0.0%: a zero win rate is a plausible figure, so nothing about it invites doubt.
    renderWith({ winRate: null, profitablePositions: 0, totalPositions: 0, unvaluedPositions: 4 })
    expect(screen.getByText('No priced positions')).toBeTruthy()
    expect(screen.queryByText('0.0%')).toBeNull()
  })

  it('does not report a drawdown of 0.00% for a range it could not measure', () => {
    // A stalled feed over the whole range leaves no measurable daily return, and "0.00%"
    // there claims the portfolio never fell.
    renderWith({ maxDrawdown: null })
    expect(screen.queryByText('0.00%')).toBeNull()
    expect(screen.getAllByText('Not enough history in this range').length).toBeGreaterThan(0)
  })
})

describe('a backend failure is stated, not silent', () => {
  it('says the backend did not respond instead of rendering nothing', () => {
    const { container } = render(<PerformanceMetricsCards metrics={null} isError />)
    expect(container.firstChild).not.toBeNull()
    expect(screen.getByText(/backend didn't respond/)).toBeTruthy()
  })

  it('still renders nothing when there is simply no data yet', () => {
    const { container } = render(<PerformanceMetricsCards metrics={null} />)
    expect(container.firstChild).toBeNull()
  })
})
