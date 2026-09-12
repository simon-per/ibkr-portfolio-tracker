// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { PositionsList } from './PositionsList'
import { WatchlistTab } from './WatchlistTab'
import { api } from '@/lib/api'
import type { Position, WatchlistItem } from '@/lib/api'

/**
 * The family test for the analyst-rating sort: one fixture of ratings, sorted through both
 * tables that rank them, must come out in one order — and descending must lead with the
 * strongest conviction, not the spelling that happens to sort last.
 *
 * Written against the *family* rather than either table because the bug was a divergence:
 * `PositionsList` scored display spellings through a switch of its own while the watchlist
 * `localeCompare`d snake_case, so the same ratings ranked differently one tab apart and
 * the watchlist's descending "Analyst" led with `strong_sell`. Both go through
 * `lib/analystRating.getRatingScore` now; a third table that ranks ratings would be caught
 * here the day it disagrees.
 */

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

/** `ScrollableTable` measures overflow with a ResizeObserver, which jsdom does not implement. */
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

/** One fixture, deliberately scrambled, with an unrated row in the middle. */
const FIXTURE: { symbol: string; rating: string | null }[] = [
  { symbol: 'HHH', rating: 'hold' },
  { symbol: 'SSS', rating: 'strong_sell' },
  { symbol: 'NNN', rating: null },
  { symbol: 'BBB', rating: 'strong_buy' },
  { symbol: 'LLL', rating: 'sell' },
  { symbol: 'UUU', rating: 'buy' },
]

/** Strongest first, unrated last: what "descending" has to mean in both tables. */
const EXPECTED_DESCENDING = ['BBB', 'UUU', 'HHH', 'LLL', 'SSS', 'NNN']

/** The positions table receives the backend's display spelling in `consensus`. */
const DISPLAY: Record<string, string> = {
  strong_buy: 'Strong Buy',
  buy: 'Buy',
  hold: 'Hold',
  sell: 'Sell',
  strong_sell: 'Strong Sell',
}

function position({ symbol, rating }: (typeof FIXTURE)[number], i: number): Position {
  return {
    security_id: i + 1, symbol, description: `${symbol} Holdings`, isin: `ISIN${i}`,
    currency: 'EUR', exchange: 'NASDAQ', quantity: 10, cost_basis_eur: 100,
    market_value_eur: 200, market_price: 20, gain_loss_eur: 100, gain_loss_percent: 100,
    taxlots: [],
    analyst_rating: rating
      ? {
          strong_buy: 0, buy: 0, hold: 0, sell: 0, strong_sell: 0, total_ratings: 0,
          consensus: DISPLAY[rating], last_updated: '2026-09-12T00:00:00Z',
        }
      : null,
  } as Position
}

/** The watchlist receives the snake_case key. */
function watchlistItem({ symbol, rating }: (typeof FIXTURE)[number], i: number): WatchlistItem {
  return {
    id: i + 1, yahoo_ticker: symbol, symbol, company_name: `${symbol} Corp`, notes: null,
    target_price: null, current_price: null, currency: null, trailing_pe: null,
    forward_pe: null, peg_ratio: null, ev_to_ebitda: null, revenue_growth: null,
    earnings_growth: null, fwd_revenue_growth: null, fwd_eps_growth: null,
    profit_margins: null, market_cap: null, analyst_target: null, analyst_rating: rating,
    analyst_count: null, week52_high: null, week52_low: null, pct_from_52w_high: null,
    ma200: null, ma50: null, pct_from_ma200: null, rsi14: null, buy_score: null,
    data_currency: null, last_synced: null, created_at: null,
  }
}

/** The fixture symbol each body row carries, in table order. */
function rowSymbols(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('tbody tr')).map((row) => {
    const text = row.textContent ?? ''
    return FIXTURE.find((f) => text.includes(f.symbol))?.symbol ?? '?'
  })
}

describe('analyst-rating sort, across both tables', () => {
  it('ranks the positions table strongest-first on a descending Rating sort', () => {
    const { container } = render(<PositionsList positions={FIXTURE.map(position)} />)
    // Default sort is market value; the first click on a new column sorts descending.
    fireEvent.click(screen.getByRole('button', { name: 'Rating' }))
    expect(rowSymbols(container)).toEqual(EXPECTED_DESCENDING)
  })

  it('ranks the watchlist strongest-first on a descending Analyst sort — it led with strong_sell', async () => {
    vi.spyOn(api, 'getWatchlist').mockResolvedValue(FIXTURE.map(watchlistItem))
    const { container } = withProviders(<WatchlistTab />)
    await screen.findByText('BBB')
    fireEvent.click(screen.getByRole('button', { name: 'Analyst' }))
    expect(rowSymbols(container)).toEqual(EXPECTED_DESCENDING)
  })

  it('agrees between the two tables in the ascending direction as well', async () => {
    const positions = render(<PositionsList positions={FIXTURE.map(position)} />)
    const rating = screen.getByRole('button', { name: 'Rating' })
    fireEvent.click(rating) // descending
    fireEvent.click(rating) // flips to ascending
    const positionsAscending = rowSymbols(positions.container)
    cleanup()

    vi.spyOn(api, 'getWatchlist').mockResolvedValue(FIXTURE.map(watchlistItem))
    const watchlist = withProviders(<WatchlistTab />)
    await screen.findByText('BBB')
    const analyst = screen.getByRole('button', { name: 'Analyst' })
    fireEvent.click(analyst)
    fireEvent.click(analyst)
    const watchlistAscending = rowSymbols(watchlist.container)

    // The watchlist keeps its nulls-last rule in either direction, so the unrated row is
    // the one place the tables legitimately differ; the five rated rows must agree.
    const rated = (order: string[]) => order.filter((s) => s !== 'NNN')
    expect(rated(watchlistAscending)).toEqual(rated(positionsAscending))
    expect(rated(positionsAscending)).toEqual(['SSS', 'LLL', 'HHH', 'UUU', 'BBB'])
  })
})
