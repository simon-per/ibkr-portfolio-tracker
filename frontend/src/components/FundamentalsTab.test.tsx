// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { FundamentalsTab } from './FundamentalsTab'
import { api } from '@/lib/api'
import type { FundamentalsStatus } from '@/lib/api'

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
