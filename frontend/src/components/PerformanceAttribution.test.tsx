// @vitest-environment jsdom
import { describe, it, expect, afterEach, beforeEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { PerformanceAttribution } from './PerformanceAttribution'
import type { PerformanceAttributionResponse } from '@/lib/api'

/**
 * The backend excludes a security it cannot value at an endpoint, rather than counting
 * it at zero — a zero end value would render as a bar of -(the whole position), which
 * on a per-security chart is the most legible place in the app to publish a fabricated
 * loss.
 *
 * Excluding is right; showing fewer bars under a total labelled as the portfolio's
 * without saying so is the same silence `unpriced_holdings` exists to end everywhere
 * else. These pin the notice, and pin that it stays away on good data.
 */

afterEach(cleanup)
// Sections remember being opened (`useRememberedOpen`); each test starts closed.
beforeEach(() => localStorage.clear())

function data(over: Partial<PerformanceAttributionResponse> = {}): PerformanceAttributionResponse {
  return {
    start_date: '2026-03-02',
    end_date: '2026-03-31',
    total_pnl_eur: 412.5,
    attributions: [
      {
        security_id: 1,
        symbol: 'AAA',
        description: 'AAA Inc',
        start_market_value_eur: 1000,
        end_market_value_eur: 1412.5,
        new_investment_eur: 0,
        value_change_eur: 412.5,
        pnl_contribution_eur: 412.5,
        contribution_percent: 100,
        weight_percent: 100,
      },
    ],
    ...over,
  }
}

describe('PerformanceAttribution completeness notice', () => {
  it('says the total covers less than the portfolio when a holding is excluded', () => {
    render(<PerformanceAttribution data={data({ unpriced_holdings: 2 })} isLoading={false} />)
    const alert = screen.getByRole('alert')
    expect(alert.textContent).toMatch(/2 holdings/)
    expect(alert.textContent).toMatch(/less than the whole portfolio/)
  })

  it('names it as missing price data rather than a loss', () => {
    // The same wording discipline as the value chart's notice: a reader seeing fewer
    // bars must not conclude those positions went to zero.
    render(<PerformanceAttribution data={data({ unpriced_holdings: 1 })} isLoading={false} />)
    expect(screen.getByRole('alert').textContent).toMatch(/not a loss/)
  })

  it('uses the singular for exactly one holding', () => {
    render(<PerformanceAttribution data={data({ unpriced_holdings: 1 })} isLoading={false} />)
    const text = screen.getByRole('alert').textContent ?? ''
    expect(text).toMatch(/1 holding could not be priced/)
    expect(text).not.toMatch(/holdings/)
  })

  it('shows no notice when everything could be priced', () => {
    render(<PerformanceAttribution data={data({ unpriced_holdings: 0 })} isLoading={false} />)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('treats an absent field as complete, not as incomplete', () => {
    // The field only exists from 2026-08-05. Reading `undefined` as incomplete would
    // badge every chart served by an older backend — the same choice the timeline's
    // own unpriced_holdings makes.
    render(<PerformanceAttribution data={data()} isLoading={false} />)
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
