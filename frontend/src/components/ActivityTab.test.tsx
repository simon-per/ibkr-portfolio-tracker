// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
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

function estimatedDividend(symbol: string, securityId: number | null = null): ActivityRow {
  return {
    date: '2026-09-10', kind: 'dividend', subtype: 'yfinance_estimate', symbol,
    security_id: securityId,
    description: `${symbol} estimated dividend`, quantity: null, price: null, currency: 'EUR',
    amount_base: 12.5, realized_pnl_base: null, counts_as_money_in: null,
    source: 'yfinance_estimate', ib_key: null, is_sale: null, is_cancellation: null,
  }
}

describe('ActivityTab row keys', () => {
  it('gives two estimated dividends on one day distinct keys', async () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.spyOn(api, 'getContributions').mockResolvedValue(contributions)
    vi.spyOn(api, 'getLogos').mockResolvedValue({ logos: {}, isins: {} })
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

describe('ActivityTab logos', () => {
  it("shows a holding's stored logo in its row, and the monogram where there is none", async () => {
    vi.spyOn(api, 'getContributions').mockResolvedValue(contributions)
    vi.spyOn(api, 'getLogos').mockResolvedValue({ logos: { '7': 'abc123' }, isins: {} })
    vi.spyOn(api, 'getActivity').mockResolvedValue({
      items: [estimatedDividend('LOGO', 7), estimatedDividend('BARE', 8)],
      total: 2, limit: 100, offset: 0, base_currency: 'EUR',
      start_date: '2025-09-12', end_date: '2026-09-12',
    })
    const { container } = withProviders(<ActivityTab />)

    await waitFor(() => expect(container.querySelector('img')).not.toBeNull())
    const images = Array.from(container.querySelectorAll('img'))
    expect(images.map((img) => img.getAttribute('src'))).toEqual([api.logoUrl(7, 'abc123')])
  })
})

function cashRow(amount: number, moneyIn: boolean, key: string): ActivityRow {
  return {
    date: '2026-09-01', kind: 'cash_flow', subtype: moneyIn ? 'DEPOSITWITHDRAW' : 'TRANSFER_IN',
    symbol: null, security_id: null, description: 'CASH', quantity: null, price: null,
    currency: 'EUR', amount_base: amount, realized_pnl_base: null, counts_as_money_in: moneyIn,
    source: null, ib_key: key, is_sale: null, is_cancellation: null,
  }
}

function page(items: ActivityRow[], total = items.length, limit = 100) {
  return {
    items, total, limit, offset: 0, base_currency: 'EUR',
    start_date: '2025-09-12', end_date: '2026-09-12',
  }
}

describe('ActivityTab badges', () => {
  it('badges a transfer amber and a deposit blue, by word and by tint', async () => {
    vi.spyOn(api, 'getContributions').mockResolvedValue(contributions)
    vi.spyOn(api, 'getActivity').mockResolvedValue(page([cashRow(500, true, 'd'), cashRow(9000, false, 't')]))
    withProviders(<ActivityTab />)

    const transfer = await screen.findByText('Transfer', { selector: 'span' })
    const deposit = screen.getByText('Deposit', { selector: 'span' })
    expect(transfer.className).toContain('bg-warn-soft')
    expect(deposit.className).toContain('bg-info-soft')
    // The glyph is decoration: the word stays the badge's whole accessible text.
    expect(transfer.querySelector('svg')?.getAttribute('aria-hidden')).toBe('true')
  })

  it("badges a sale and its cancellation from the server's flags, not the subtype", async () => {
    const trade = (key: string, subtype: string, isSale: boolean, cancelled: boolean): ActivityRow => ({
      date: '2026-09-02', kind: 'trade', subtype, symbol: 'AAA', security_id: null,
      description: subtype, quantity: 1, price: 10, currency: 'EUR', amount_base: 10,
      realized_pnl_base: null, counts_as_money_in: null, source: null, ib_key: key,
      is_sale: isSale, is_cancellation: cancelled,
    })
    vi.spyOn(api, 'getContributions').mockResolvedValue(contributions)
    vi.spyOn(api, 'getLogos').mockResolvedValue({ logos: {}, isins: {} })
    vi.spyOn(api, 'getActivity').mockResolvedValue(page([
      trade('s', 'SELL', true, false),
      trade('sc', 'CANCELSELL', true, true),
      trade('bc', 'CANCELBUY', false, true),
    ]))
    withProviders(<ActivityTab />)

    expect(await screen.findByText('Sell', { selector: 'span' })).toBeTruthy()
    expect(screen.getByText('Sell · cancelled', { selector: 'span' })).toBeTruthy()
    expect(screen.getByText('Buy · cancelled', { selector: 'span' })).toBeTruthy()
  })
})

describe('ActivityTab figures strip', () => {
  it('sums a window that fits on one page, leaving a transfer out of net deposits', async () => {
    vi.spyOn(api, 'getContributions').mockResolvedValue(contributions)
    const getActivity = vi.spyOn(api, 'getActivity').mockResolvedValue(
      page([cashRow(500, true, 'd'), cashRow(-200, true, 'w'), cashRow(9000, false, 't')]),
    )
    withProviders(<ActivityTab />)

    const tile = (await screen.findByText('Net deposits')).closest('div.flex.flex-col') as HTMLElement
    expect(tile.textContent).toMatch(/\+€300\.00/)
    expect(getActivity).toHaveBeenCalledTimes(1)
  })

  it('fetches the whole window once when it is longer than a page', async () => {
    vi.spyOn(api, 'getContributions').mockResolvedValue(contributions)
    const getActivity = vi.spyOn(api, 'getActivity').mockImplementation(async (params) =>
      params?.limit === 500
        ? page([cashRow(500, true, 'a'), cashRow(700, true, 'b')], 2, 500)
        : page([cashRow(500, true, 'a')], 2),
    )
    withProviders(<ActivityTab />)

    await waitFor(() => {
      const tile = screen.getByText('Net deposits').closest('div.flex.flex-col') as HTMLElement
      expect(tile.textContent).toMatch(/\+€1,200\.00/)
    })
    expect(getActivity.mock.calls.map(([p]) => p?.limit).sort()).toEqual([100, 500])
  })

  it('shows dashes, and asks nothing more, past the endpoint ceiling', async () => {
    vi.spyOn(api, 'getContributions').mockResolvedValue(contributions)
    const getActivity = vi.spyOn(api, 'getActivity').mockResolvedValue(page([cashRow(500, true, 'a')], 900))
    withProviders(<ActivityTab />)

    const tile = (await screen.findByText('Net deposits')).closest('div.flex.flex-col') as HTMLElement
    expect(tile.textContent).toContain('—')
    expect(screen.getByText('900')).toBeTruthy()
    expect(getActivity).toHaveBeenCalledTimes(1)
  })
})
