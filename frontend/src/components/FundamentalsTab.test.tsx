// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { FundamentalsTab } from './FundamentalsTab'
import { api } from '@/lib/api'
import type { FundamentalMetrics, FundamentalsStatus } from '@/lib/api'

/**
 * EPS figures carried a hardcoded `$` in three places. EPS is reported in the issuer's own
 * currency, which neither earnings row carries, so a dollar sign on a Nestlé or an SK Hynix
 * figure was a claim the data could not back — the same shape as the `€` the currency
 * context exists to avoid. The header already says EPS; the figure is bare.
 */

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

/**
 * `ScrollableTable` measures overflow with a ResizeObserver, and this tab constructs one of
 * its own to size the history page; jsdom implements neither.
 */
beforeEach(() => {
  if (!('ResizeObserver' in globalThis)) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
  }
})

function withProviders(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>)
}

const STATUS: FundamentalsStatus = {
  total_securities: 1,
  securities_with_data: 1,
  securities_without_data: 0,
  stale_metrics: 0,
  total_earnings_events: 2,
  oldest_update: null,
  newest_update: null,
}

describe('FundamentalsTab EPS figures', () => {
  it("prints EPS as a bare figure, since the issuer's currency is not known here", async () => {
    vi.spyOn(api, 'getPortfolioFundamentals').mockResolvedValue([])
    vi.spyOn(api, 'getUpcomingEarnings').mockResolvedValue([
      { security_id: 1, symbol: 'NESN', description: 'Nestlé SA', earnings_date: '2026-10-15', eps_estimate: 1.23 },
    ])
    vi.spyOn(api, 'getEarningsHistory').mockResolvedValue([
      {
        security_id: 1, symbol: 'NESN', description: 'Nestlé SA', earnings_date: '2026-07-24',
        eps_estimate: 2.45, reported_eps: 2.5, surprise_percent: 2.0, beat_or_miss: 'Beat',
      },
    ])
    vi.spyOn(api, 'getFundamentalsStatus').mockResolvedValue(STATUS)
    withProviders(<FundamentalsTab />)

    // The calendar's estimate and the history's two columns.
    expect((await screen.findByText(/Est: 1\.23/)).textContent).not.toContain('$')
    expect(await screen.findByText('2.45')).toBeTruthy()
    expect(screen.getByText('2.50')).toBeTruthy()
    // And nothing on the tab claims a dollar figure anywhere.
    expect(document.body.textContent).not.toMatch(/\$\s?\d/)
  })
})

const NOW = Date.parse('2026-10-14T10:00:00Z')

function metrics(over: Partial<FundamentalMetrics>): FundamentalMetrics {
  return {
    security_id: 1, symbol: 'AAA', description: 'A', exchange: 'NASDAQ', currency: 'USD',
    quote_type: 'EQUITY', trailing_pe: null, forward_pe: null, peg_ratio: null,
    price_to_sales: null, price_to_book: null, revenue_growth: null, earnings_growth: null,
    fwd_revenue_growth: null, fwd_eps_growth: null, profit_margins: null, gross_margins: null,
    operating_margins: null, market_cap: null, market_cap_base: null, number_of_analysts: null,
    target_mean_price: null, target_high_price: null, target_low_price: null,
    data_currency: 'USD', last_updated: '2026-10-11T02:05:00+00:00',
    ...over,
  }
}

function renderWith(rows: FundamentalMetrics[]) {
  vi.spyOn(Date, 'now').mockReturnValue(NOW)
  vi.spyOn(api, 'getPortfolioFundamentals').mockResolvedValue(rows)
  vi.spyOn(api, 'getUpcomingEarnings').mockResolvedValue([])
  vi.spyOn(api, 'getEarningsHistory').mockResolvedValue([])
  vi.spyOn(api, 'getFundamentalsStatus').mockResolvedValue(STATUS)
  return withProviders(<FundamentalsTab />)
}

function bodySymbols(): string[] {
  const table = screen.getAllByRole('table')[0]
  return within(table)
    .getAllByRole('row')
    .slice(1)
    .map((row) => row.querySelector('td div')?.textContent ?? '')
}

describe('FundamentalsTab figures', () => {
  it('colours a negative P/E as a loss, never as cheap', async () => {
    renderWith([metrics({ trailing_pe: -8.2 }), metrics({ security_id: 2, symbol: 'BBB', trailing_pe: 9 })])
    const negative = await screen.findByText('-8.2')
    expect(negative.closest('td')?.className).toContain('text-red-600')
    expect(screen.getByText('9.0').closest('td')?.className).toContain('text-green-600')
  })

  it('prints an absent figure as an em dash', async () => {
    renderWith([metrics({})])
    await screen.findAllByText('AAA')
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
    expect(within(screen.getAllByRole('table')[0]).queryByText('-')).toBeNull()
  })

  it('sorts market cap on the base-currency figure, not the mixed raw numbers', async () => {
    renderWith([
      // 1 trillion TWD is the largest raw number and the smallest company.
      metrics({ security_id: 1, symbol: 'TWN', market_cap: 1e12, data_currency: 'TWD', market_cap_base: 2.6e10 }),
      metrics({ security_id: 2, symbol: 'USA', market_cap: 5e11, data_currency: 'USD', market_cap_base: 4.2e11 }),
      metrics({ security_id: 3, symbol: 'GBX', market_cap: 9e11, data_currency: 'GBp', market_cap_base: null }),
    ])
    await screen.findAllByText('TWN')
    fireEvent.click(screen.getAllByRole('button', { name: /Mkt Cap/ })[0])
    // Descending on the converted figure, the unconvertible one last.
    expect(bodySymbols()).toEqual(['USA', 'TWN', 'GBX'])
  })

  it('states how old the data is, and turns amber once a weekly pass was missed', async () => {
    renderWith([metrics({}), metrics({ security_id: 2, symbol: 'BBB', last_updated: null })])
    const line = await screen.findByText(/^Data as of /)
    expect(line.className).not.toContain('text-amber')

    cleanup()
    vi.restoreAllMocks()
    renderWith([metrics({ last_updated: '2026-09-30T02:05:00+00:00' })])
    expect((await screen.findByText(/^Data as of /)).className).toContain('text-amber')
  })
})
