// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ActivityTab } from './ActivityTab'
import { api } from '@/lib/api'
import type { ActivityRow, ContributionsResponse } from '@/lib/api'

/**
 * Row identity. Estimated dividends carry no `ib_key` and cluster on dates — every payer
 * projected onto the same ex-date lands on one day — and the fallback key was `kind-date`,
 * so two of them shared a React key. React reports that as a console error and then
 * reconciles the rows by guesswork; the error is the observable, so that is what is
 * asserted. The key carries the symbol and the row's position in the page now.
 */

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

// `ScrollableTable` measures overflow with a ResizeObserver, which jsdom does not implement.
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

/** Only the field this tab reads from the contributions response. */
const contributions = { first_contribution_date: '2024-05-28' } as unknown as ContributionsResponse

function estimatedDividend(symbol: string): ActivityRow {
  return {
    date: '2026-09-10', kind: 'dividend', subtype: 'yfinance_estimate', symbol,
    description: `${symbol} estimated dividend`, quantity: null, price: null, currency: 'EUR',
    amount_base: 12.5, realized_pnl_base: null, counts_as_money_in: null,
    source: 'yfinance_estimate', ib_key: null,
  }
}

describe('ActivityTab row keys', () => {
  it('gives two estimated dividends on one day distinct keys', async () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.spyOn(api, 'getContributions').mockResolvedValue(contributions)
    vi.spyOn(api, 'getActivity').mockResolvedValue({
      items: [estimatedDividend('AAA'), estimatedDividend('BBB')],
      total: 2, limit: 100, offset: 0, base_currency: 'EUR',
      start_date: '2025-09-12', end_date: '2026-09-12',
    })
    withProviders(<ActivityTab />)

    await screen.findAllByText(/AAA/)
    expect(screen.getAllByText(/BBB/).length).toBeGreaterThan(0)
    const duplicateKeys = errors.mock.calls.filter((args) => String(args[0]).includes('same key'))
    expect(duplicateKeys).toEqual([])
  })
})
