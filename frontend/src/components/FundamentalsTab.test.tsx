// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { FundamentalsTab } from './FundamentalsTab'
import { api } from '@/lib/api'
import type { EarningsHistoryItem, FundamentalMetrics, FundamentalsStatus } from '@/lib/api'

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

function withProviders(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>)
}

const STATUS: FundamentalsStatus = {
  total_securities: 1,
  securities_with_data: 1,
  securities_without_data: 0,
  securities_unavailable: 0,
  stale_metrics: 0,
  total_earnings_events: 2,
  oldest_update: null,
  newest_update: null,
  sync_running: false,
}

describe('FundamentalsTab EPS figures', () => {
  it("prints EPS as a bare figure, since the issuer's currency is not known here", async () => {
    vi.spyOn(api, 'getPortfolioFundamentals').mockResolvedValue([])
    vi.spyOn(api, 'getUpcomingEarnings').mockResolvedValue([
      { security_id: 1, symbol: 'NESN', symbols: ['NESN'], description: 'Nestlé SA', earnings_date: '2026-10-15T05:00:00+00:00', eps_estimate: 1.23 },
    ])
    vi.spyOn(api, 'getEarningsHistory').mockResolvedValue([
      {
        security_id: 1, symbol: 'NESN', symbols: ['NESN'], description: 'Nestlé SA', earnings_date: '2026-07-24T05:00:00+00:00',
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

describe('FundamentalsTab earnings rows', () => {
  it("names every held listing of a company once, and dates both lists by the same instant", async () => {
    // Late in the UTC day, so a parse as local time would land on another day somewhere.
    const upcoming = '2026-10-28T23:30:00+00:00'
    const past = '2026-07-23T23:30:00+00:00'
    vi.spyOn(api, 'getPortfolioFundamentals').mockResolvedValue([])
    vi.spyOn(api, 'getUpcomingEarnings').mockResolvedValue([
      { security_id: 3, symbol: 'GOOG', symbols: ['GOOG', 'ABEA', 'GOOGL'], description: 'Alphabet',
        earnings_date: upcoming, eps_estimate: 2.2 },
    ])
    vi.spyOn(api, 'getEarningsHistory').mockResolvedValue([
      { security_id: 3, symbol: 'GOOG', symbols: ['GOOG', 'ABEA', 'GOOGL'], description: 'Alphabet',
        earnings_date: past, eps_estimate: 2.0, reported_eps: 2.1, surprise_percent: 5.0, beat_or_miss: 'Beat' },
    ])
    vi.spyOn(api, 'getFundamentalsStatus').mockResolvedValue(STATUS)
    withProviders(<FundamentalsTab />)

    expect(await screen.findAllByText('GOOG · ABEA · GOOGL')).toHaveLength(2)
    // The viewer's own day of each instant, whatever zone the runner is in.
    const day = (iso: string, weekday: boolean) => new Date(iso).toLocaleDateString('en-US', {
      ...(weekday && { weekday: 'short' as const }), year: 'numeric', month: 'short', day: 'numeric',
    })
    expect(screen.getByText(day(upcoming, true))).toBeTruthy()
    expect(screen.getByText(day(past, false))).toBeTruthy()
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
    .map((row) => row.querySelector('td .font-medium')?.textContent ?? '')
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

  it('shows the market cap in the base currency, and its own figure only when it cannot convert', async () => {
    renderWith([
      metrics({ security_id: 1, symbol: 'TWN', market_cap: 1e12, data_currency: 'TWD', market_cap_base: 2.6e10 }),
      metrics({ security_id: 3, symbol: 'GBX', market_cap: 9e11, data_currency: 'GBp', market_cap_base: null }),
    ])
    const table = within((await screen.findAllByRole('table'))[0])
    // Converted: the base currency's prefix, and no native code beside it.
    expect(table.getByText('€26.0B')).toBeTruthy()
    expect(table.queryByText(/TWD/)).toBeNull()
    // Not convertible: the listing's own figure, labelled with its own code.
    expect(table.getByText('900.0B')).toBeTruthy()
    expect(table.getByText(/GBp/)).toBeTruthy()
  })

  it('states how old the data is, and turns amber once a daily pass was missed', async () => {
    renderWith([metrics({}), metrics({ security_id: 2, symbol: 'BBB', last_updated: null })])
    const line = await screen.findByText(/^Data as of /)
    expect(line.className).not.toContain('text-amber')

    cleanup()
    vi.restoreAllMocks()
    renderWith([metrics({ last_updated: '2026-09-30T02:05:00+00:00' })])
    expect((await screen.findByText(/^Data as of /)).className).toContain('text-amber')
  })
})

describe('FundamentalsTab background sync', () => {
  // The banner's end is derived from the status poll (no state cleared in an effect): a
  // sync is finished once the newest row is newer than the moment it started, and the
  // table is fetched once more. Real timers — the poll runs every 5 s — hence the timeout.
  it('polls until a row newer than the sync appears, then clears the banner and refetches', async () => {
    let newest: string | null = '2026-10-01T00:00:00Z'
    const rows = vi.spyOn(api, 'getPortfolioFundamentals').mockResolvedValue([metrics({})])
    vi.spyOn(api, 'getUpcomingEarnings').mockResolvedValue([])
    vi.spyOn(api, 'getEarningsHistory').mockResolvedValue([])
    // The pass is running until it writes, so only the newer row can end the banner.
    vi.spyOn(api, 'getFundamentalsStatus').mockImplementation(async () => ({
      ...STATUS,
      newest_update: newest,
      sync_running: true,
    }))
    vi.spyOn(api, 'syncFundamentals').mockResolvedValue(
      { status: 'started' } as Awaited<ReturnType<typeof api.syncFundamentals>>,
    )
    withProviders(<FundamentalsTab />)
    await screen.findAllByText('AAA')

    // Every holding has data, so the button offers a forced refresh.
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText(/Updating — the table refreshes/)).toBeTruthy()
    const fetchesBefore = rows.mock.calls.length

    // The sync writes a row; the next poll sees it.
    newest = new Date(Date.now() + 60_000).toISOString()
    await waitFor(
      () => expect(screen.queryByText(/Updating — the table refreshes/)).toBeNull(),
      { timeout: 9000 },
    )
    await waitFor(() => expect(rows.mock.calls.length).toBeGreaterThan(fetchesBefore))
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeTruthy()
  }, 20_000)
})

describe('FundamentalsTab earnings surprise history', () => {
  const quarter = (i: number): EarningsHistoryItem => ({
    security_id: i, symbol: `S${i}`, symbols: [`S${i}`], description: `S${i} Corp`,
    earnings_date: `2026-07-${String(1 + (i % 28)).padStart(2, '0')}T05:00:00+00:00`,
    eps_estimate: 1, reported_eps: 1.1, surprise_percent: 10, beat_or_miss: 'Beat',
  })

  it('stays on a page that exists when a shorter history arrives (not "Page 3 of 2")', async () => {
    const history = Array.from({ length: 25 }, (_, i) => quarter(i + 1))
    vi.spyOn(api, 'getPortfolioFundamentals').mockResolvedValue([])
    vi.spyOn(api, 'getUpcomingEarnings').mockResolvedValue([])
    vi.spyOn(api, 'getEarningsHistory').mockResolvedValue(history)
    vi.spyOn(api, 'getFundamentalsStatus').mockResolvedValue(STATUS)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><FundamentalsTab /></QueryClientProvider>)

    expect(await screen.findByText('Page 1 of 3')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Next/ }))
    fireEvent.click(screen.getByRole('button', { name: /Next/ }))
    expect(screen.getByText('Page 3 of 3')).toBeTruthy()

    // A refetch after a sync returns twelve quarters: two pages, and the last one has rows.
    act(() => client.setQueryData(['fundamentals', 'earnings', 'history'], history.slice(0, 12)))
    expect(await screen.findByText('Page 2 of 2')).toBeTruthy()
    expect(screen.getAllByText('S11').length).toBeGreaterThan(0)
    expect(screen.getAllByText('S12').length).toBeGreaterThan(0)
    // And Previous still steps back from where the reader is.
    fireEvent.click(screen.getByRole('button', { name: /Prev/ }))
    expect(screen.getByText('Page 1 of 2')).toBeTruthy()
  })
})
