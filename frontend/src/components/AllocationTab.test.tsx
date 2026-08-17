// @vitest-environment jsdom
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AllocationTab } from './AllocationTab'
import { CurrencyProvider } from '@/lib/CurrencyContext'
import { api } from '@/lib/api'
import type { PortfolioAllocationResponse } from '@/lib/api'

/**
 * The completeness caveat, which is the one thing on this tab that nothing else can reveal.
 *
 * Every slice here is labelled "% of portfolio" and the three breakdowns sum to exactly 100
 * — but they are shares of the value that could be *priced*, so a holding the backend
 * cannot value is not a visible zero, it is an **absence** from a picture that looks
 * complete. `test_allocation_completeness.py` pinned the sums for months and was blind to
 * it, because summing to 100 is what being wrong looks like here.
 *
 * Live rather than theoretical: three newly bought ETFs were unpriced for hours on
 * 2026-08-07.
 */

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

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
  return render(
    <QueryClientProvider client={client}>
      <CurrencyProvider>{node}</CurrencyProvider>
    </QueryClientProvider>,
  )
}

function allocation(
  over: Partial<PortfolioAllocationResponse> = {},
): PortfolioAllocationResponse {
  return {
    sector_allocation: {
      Technology: {
        percentage: 100,
        market_value_eur: 1000,
        positions: [
          {
            symbol: 'AAPL',
            description: 'Apple Inc',
            weight: 100,
            market_value_eur: 1000,
            is_etf_contribution: false,
          },
        ],
      },
    },
    geographic_allocation: {
      'United States': {
        percentage: 100,
        market_value_eur: 1000,
        positions: [
          {
            symbol: 'AAPL',
            description: 'Apple Inc',
            weight: 100,
            market_value_eur: 1000,
            is_etf_contribution: false,
          },
        ],
      },
    },
    asset_type_allocation: {
      Stock: {
        percentage: 100,
        market_value_eur: 1000,
        positions: [
          {
            symbol: 'AAPL',
            description: 'Apple Inc',
            weight: 100,
            market_value_eur: 1000,
            is_etf_contribution: false,
          },
        ],
      },
    },
    total_market_value_eur: 1000,
    unpriced_holdings: 0,
    unpriced_symbols: [],
    ...over,
  }
}

const status = {
  total_securities: 1,
  securities_with_data: 1,
  securities_without_data: 0,
  stale_securities: 0,
}

function mockApi(over: Partial<PortfolioAllocationResponse> = {}) {
  vi.spyOn(api, 'getPortfolioAllocation').mockResolvedValue(allocation(over))
  vi.spyOn(api, 'getAllocationStatus').mockResolvedValue(
    status as Awaited<ReturnType<typeof api.getAllocationStatus>>,
  )
  vi.spyOn(api, 'getPositions').mockResolvedValue([])
}

describe('AllocationTab completeness', () => {
  it('names the holdings the charts exclude, as an alert above them', async () => {
    mockApi({ unpriced_holdings: 2, unpriced_symbols: ['SBI', 'MBGL'] })
    withProviders(<AllocationTab />)

    const alert = await waitFor(() => screen.getByRole('alert'))
    expect(alert.textContent).toMatch(/exclude 2 holdings with no usable price/)
    // The symbols, so the reader can go and fix the mapping rather than hunt for them.
    expect(alert.textContent).toMatch(/SBI, MBGL/)
    // And what the percentages therefore mean, since they still sum to 100.
    expect(alert.textContent).toMatch(/share of what could be valued/)
  })

  it('says nothing when every holding could be valued', async () => {
    mockApi()
    withProviders(<AllocationTab />)

    await waitFor(() => expect(screen.getByText('Technology')).toBeTruthy())
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('reads a backend with no such field as complete', async () => {
    // Absent means an older backend, not an unmeasurable one — the same choice the
    // timeline's own field makes. Otherwise every chart carries a permanent warning.
    mockApi({ unpriced_holdings: undefined, unpriced_symbols: undefined })
    withProviders(<AllocationTab />)

    await waitFor(() => expect(screen.getByText('Technology')).toBeTruthy())
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('uses the singular for one holding', async () => {
    mockApi({ unpriced_holdings: 1, unpriced_symbols: ['SBI'] })
    withProviders(<AllocationTab />)

    const alert = await waitFor(() => screen.getByRole('alert'))
    expect(alert.textContent).toMatch(/1 holding with no usable price/)
  })
})
