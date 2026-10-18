// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { FundamentalsTab } from './FundamentalsTab'
import { api } from '@/lib/api'
import type { FundamentalMetrics, FundamentalsStatus } from '@/lib/api'

/**
 * GitHub issue #18. The "Updating" state ended only on a row newer than the click, so a
 * background pass that wrote nothing — every security fresh, a Yahoo 429 at the first
 * one, refused as busy, every security erroring — left the banner up, both buttons
 * disabled and `/status` polled every 5 s for as long as the tab was open. And a refused
 * Refresh showed nothing at all; only a refused Update did.
 */

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

function withProviders(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>)
}

const ROW: FundamentalMetrics = {
  security_id: 1, symbol: 'AAA', description: 'A', exchange: 'NASDAQ', currency: 'USD',
  quote_type: 'EQUITY', trailing_pe: null, forward_pe: null, peg_ratio: null,
  price_to_sales: null, price_to_book: null, revenue_growth: null, earnings_growth: null,
  fwd_revenue_growth: null, fwd_eps_growth: null, profit_margins: null, gross_margins: null,
  operating_margins: null, market_cap: null, market_cap_base: null, number_of_analysts: null,
  target_mean_price: null, target_high_price: null, target_low_price: null,
  data_currency: 'USD', last_updated: '2026-10-11T02:05:00+00:00',
}

// One holding without data, so the "Update" button is the one shown.
const STATUS: FundamentalsStatus = {
  total_securities: 2,
  securities_with_data: 1,
  securities_without_data: 1,
  securities_unavailable: 0,
  stale_metrics: 0,
  total_earnings_events: 0,
  oldest_update: '2026-10-11T02:05:00Z',
  newest_update: '2026-10-11T02:05:00Z',
  sync_running: false,
}

const BANNER = /Updating — the table refreshes/

/** `newest_update` never moves: the background pass wrote nothing. */
function mockReads(status: FundamentalsStatus = STATUS) {
  vi.spyOn(api, 'getPortfolioFundamentals').mockResolvedValue([ROW])
  vi.spyOn(api, 'getUpcomingEarnings').mockResolvedValue([])
  vi.spyOn(api, 'getEarningsHistory').mockResolvedValue([])
  return vi.spyOn(api, 'getFundamentalsStatus').mockResolvedValue(status)
}

async function startSync() {
  vi.spyOn(api, 'syncFundamentals').mockResolvedValue(
    { status: 'started' } as Awaited<ReturnType<typeof api.syncFundamentals>>,
  )
  withProviders(<FundamentalsTab />)
  await screen.findAllByText('AAA')
  fireEvent.click(screen.getByRole('button', { name: 'Update' }))
  expect(await screen.findByText(BANNER)).toBeTruthy()
}

/**
 * Let `minutes` pass, one poll interval per `act`. Every poll now renders (its `askedAt`
 * is new), and dozens of renders inside a single `act` trip React's nested-update limit —
 * an artifact of compressing minutes into one tick; real polls are 5 s apart.
 */
async function elapse(minutes: number) {
  for (let t = 0; t < minutes * 60_000; t += 5_000) {
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000) })
  }
}

function updateButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: /Updat/ }) as HTMLButtonElement
}

describe('a background sync that writes no row', () => {
  it('ends on the first poll after the click that finds no pipeline running', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const status = mockReads()
    await startSync()

    // Thirty minutes later — far longer than any fundamentals pass takes.
    await elapse(30)

    expect(screen.queryByText(BANNER)).toBeNull()
    expect(updateButton().disabled).toBe(false)
    // The initial read, one poll, and the refetch on finishing — then polling stops.
    expect(status.mock.calls.length).toBeLessThan(10)
  }, 20_000)

  it('ends at the deadline when the backend keeps reporting a sync running', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const status = mockReads({ ...STATUS, sync_running: true })
    await startSync()

    // Still running after ten minutes: a legitimate pass takes several.
    await elapse(10)
    expect(screen.queryByText(BANNER)).toBeTruthy()

    await elapse(6)
    expect(screen.queryByText(BANNER)).toBeNull()
    expect(updateButton().disabled).toBe(false)

    // And the poll has stopped, not merely the banner.
    const polls = status.mock.calls.length
    await elapse(10)
    expect(status.mock.calls.length).toBe(polls)
  }, 20_000)
})

describe('a refused forced refresh', () => {
  it('says so, as a refused Update does', async () => {
    mockReads({ ...STATUS, securities_without_data: 0, securities_with_data: 2 })
    vi.spyOn(api, 'syncFundamentals').mockRejectedValue(
      new Error('A sync started moments ago; retry in ~240s'),
    )
    withProviders(<FundamentalsTab />)
    await screen.findAllByText('AAA')

    fireEvent.click(await screen.findByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText(/retry in ~240s/, {}, { timeout: 2000 })).toBeTruthy()
  })
})
