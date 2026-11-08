// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { WatchlistTab } from './WatchlistTab'
import { colorClass, signalTone } from './watchlistColumns'
import { api } from '@/lib/api'
import type { WatchlistItem } from '@/lib/api'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function item(id: number, over: Partial<WatchlistItem>): WatchlistItem {
  return {
    id, yahoo_ticker: `T${id}`, symbol: `T${id}`, company_name: null, notes: null,
    target_price: null, current_price: 10, currency: 'USD', trailing_pe: null, forward_pe: null,
    peg_ratio: null, ev_to_ebitda: null, revenue_growth: null, earnings_growth: null,
    fwd_revenue_growth: null, fwd_eps_growth: null, profit_margins: null, market_cap: null,
    market_cap_base: null, analyst_target: null, analyst_rating: null, analyst_count: null,
    week52_high: null, week52_low: null, pct_from_52w_high: null, ma200: null, ma50: null,
    pct_from_ma200: null, rsi14: null, buy_score: null, data_currency: 'USD',
    last_synced: '2026-10-17T04:00:00', created_at: null, ...over,
  }
}

describe('signalTone', () => {
  it('keeps the watchlist rule: ±5 is plain, below a high is the good side', () => {
    expect(signalTone(null)).toBe('muted')
    expect(signalTone(-4.9)).toBe('neutral')
    expect(signalTone(-20)).toBe('positive')
    expect(signalTone(20)).toBe('negative')
    expect(signalTone(20, true)).toBe('positive')
  })

  it('is what colorClass paints', () => {
    expect(colorClass(-20)).toBe('text-green-600 dark:text-green-400')
    expect(colorClass(2)).toBe('text-foreground')
    expect(colorClass(null)).toBe('text-muted-foreground')
  })
})

describe('WatchlistTab', () => {
  it('counts the list into the Score column’s three bands, and draws distances as pills', async () => {
    vi.spyOn(api, 'getWatchlist').mockResolvedValue([
      item(1, { buy_score: 82, pct_from_52w_high: -30 }),
      item(2, { buy_score: 71 }),
      item(3, { buy_score: 50 }),
      item(4, { buy_score: null }),
    ])
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><WatchlistTab /></QueryClientProvider>)

    const mix = await screen.findByRole('list', { name: 'Stocks by buy score' })
    expect(within(mix).getByLabelText('Score 70+: 2')).toBeTruthy()
    expect(within(mix).getByLabelText('Score 40–69: 1')).toBeTruthy()
    expect(within(mix).getByLabelText('Score under 40: 0')).toBeTruthy()

    const pill = screen.getByText('-30.0%')
    expect(pill.className).toContain('bg-gain-soft')
  })
})

describe('editing a watchlist row', () => {
  it('clears a note and a target price by emptying them (review item U2)', async () => {
    vi.spyOn(api, 'getWatchlist').mockResolvedValue([
      item(1, { symbol: 'AAA', notes: 'buy below 90', target_price: 90 }),
    ])
    const update = vi.spyOn(api, 'updateWatchlistItem').mockResolvedValue(item(1, {}))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><WatchlistTab /></QueryClientProvider>)

    fireEvent.click(await screen.findByRole('button', { name: /Edit notes and target price for AAA/ }))
    fireEvent.change(screen.getByLabelText('Notes'), { target: { value: '' } })
    fireEvent.change(screen.getByLabelText(/Target Price/), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await vi.waitFor(() => expect(update).toHaveBeenCalledWith(1, '', null))
  })
})

describe('a failed save', () => {
  it('does not greet the next edit with its error (review item U8)', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.spyOn(api, 'getWatchlist').mockResolvedValue([item(1, { symbol: 'AAA' })])
    vi.spyOn(api, 'updateWatchlistItem').mockRejectedValue(new Error('500'))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    render(<QueryClientProvider client={client}><WatchlistTab /></QueryClientProvider>)
    const edit = await screen.findByRole('button', { name: /Edit notes and target price for AAA/ })

    fireEvent.click(edit)
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText(/Failed to save/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    fireEvent.click(edit)
    expect(screen.queryByText(/Failed to save/)).toBeNull()
  })
})
