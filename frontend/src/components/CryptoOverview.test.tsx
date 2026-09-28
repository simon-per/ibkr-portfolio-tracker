// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  api,
  ForbiddenError,
  UnauthorizedError,
  type CryptoHistoryResponse,
  type CryptoPortfolioResponse,
} from '@/lib/api'
import { CurrencyProvider } from '@/lib/CurrencyContext'
import { CryptoOverview } from './CryptoOverview'

// jsdom cannot measure charts. Keep every control, caption and transform real.
vi.mock('recharts', async () => {
  const actual = await vi.importActual<typeof import('recharts')>('recharts')
  return { ...actual, ResponsiveContainer: () => <div data-testid="chart" /> }
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  })
  vi.spyOn(api, 'getSettings').mockResolvedValue({
    base_currency: 'CHF', supported_currencies: ['EUR', 'CHF', 'USD'],
    dividend_forecast_withholding_pct: 15,
  })
})

/** Invented figures, like every crypto fixture in this repo. */
function book(over: Partial<CryptoPortfolioResponse> = {}): CryptoPortfolioResponse {
  return {
    configured: true, base_currency: 'CHF', as_of: '2026-09-20T12:00:00+00:00',
    total_value: 1000, itemised_value: 900, unitemised_value: 100, defi_value: null,
    total_cost: 700, unrealized_pl: 200, unrealized_pl_pct: 28.57, realized_pl: -10,
    realized_pl_pct: -1.4, all_time_pl: 190, all_time_pl_pct: 27.1, change_24h: 12,
    change_24h_pct: 1.35, fx_caveat: "Cost and P&L are CoinStats' USD figures converted at the 2026-09-20 rate; FX moves since purchase are not in them.",
    fx_unavailable: 0, valued_count: 2, spam_count: 3, unpriced_count: 1,
    unpriced_symbols: ['MYST'],
    holdings: [
      {
        coin_id: 'bitcoin', symbol: 'BTC', name: 'Bitcoin', rank: 1, is_fiat: false,
        status: 'valued', quantity: 0.01, price: 50_000, value: 500, weight_pct: 50,
        change_24h_pct: 2, avg_buy: 40_000, total_cost: 400, unrealized_pl: 100,
        unrealized_pl_pct: 25, realized_pl: 0,
      },
      {
        coin_id: 'FiatCoinEUR', symbol: 'EUR', name: 'Euro', rank: null, is_fiat: true,
        status: 'valued', quantity: 400, price: 1, value: 400, weight_pct: 40,
        change_24h_pct: 0, avg_buy: null, total_cost: null, unrealized_pl: null,
        unrealized_pl_pct: null, realized_pl: null,
      },
      {
        coin_id: 'mystery-token', symbol: 'MYST', name: 'Mystery', rank: null, is_fiat: false,
        status: 'unpriced', quantity: 42, price: null, value: null, weight_pct: null,
        change_24h_pct: null, avg_buy: null, total_cost: null, unrealized_pl: null,
        unrealized_pl_pct: null, realized_pl: null,
      },
    ],
    color_order: ['bitcoin', 'FiatCoinEUR'],
    warnings: ['CoinStats credits are low: 3000 of 20000 left this period.'],
    ...over,
  }
}

const HISTORY: CryptoHistoryResponse = {
  configured: true, base_currency: 'CHF', fetched_at: '2026-09-20T06:00:00+00:00',
  fx_caveat: null, fx_unavailable: 0, warnings: [],
  points: [
    { date: '2026-09-18', value: 950, pnl: 150 },
    { date: '2026-09-19', value: null, pnl: null },
    { date: '2026-09-20', value: 1000, pnl: 190 },
  ],
}

function renderOverview(serverHasNoKey = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <CurrencyProvider>
        <CryptoOverview serverHasNoKey={serverHasNoKey} />
      </CurrencyProvider>
    </QueryClientProvider>,
  )
}

describe('CryptoOverview states', () => {
  it('says the server has no key without asking for data', async () => {
    const portfolio = vi.spyOn(api, 'getCryptoPortfolio')
    renderOverview(true)
    expect(await screen.findByText('Crypto needs an admin key on the server')).toBeTruthy()
    expect(portfolio).not.toHaveBeenCalled()
  })

  it('treats a 403 the same way — no browser key could open it', async () => {
    vi.spyOn(api, 'getCryptoPortfolio').mockRejectedValue(new ForbiddenError('no key configured'))
    renderOverview()
    expect(await screen.findByText('Crypto needs an admin key on the server')).toBeTruthy()
  })

  it('points a locked browser at the lock button in its own words', async () => {
    vi.spyOn(api, 'getCryptoPortfolio').mockRejectedValue(new UnauthorizedError('generic'))
    renderOverview()
    expect(await screen.findByText('Crypto is locked')).toBeTruthy()
    expect(screen.getByText(/lock button/)).toBeTruthy()
    expect(screen.queryByText('generic')).toBeNull()
  })

  it('explains an unconfigured server', async () => {
    vi.spyOn(api, 'getCryptoPortfolio').mockResolvedValue(book({ configured: false, as_of: null, holdings: [] }))
    renderOverview()
    expect(await screen.findByText('CoinStats is not configured')).toBeTruthy()
    expect(screen.getByText('COIN_STATS_SHARE_TOKEN')).toBeTruthy()
  })

  it('asks for a first sync when configured but empty', async () => {
    vi.spyOn(api, 'getCryptoPortfolio').mockResolvedValue(book({ as_of: null, holdings: [] }))
    const history = vi.spyOn(api, 'getCryptoHistory')
    renderOverview()
    expect(await screen.findByText('No crypto sync has run yet')).toBeTruthy()
    expect(history).not.toHaveBeenCalled()
  })

  it('reports a failed load as a failure, not as an empty book', async () => {
    vi.spyOn(api, 'getCryptoPortfolio').mockRejectedValue(new Error('HTTP 502: Bad Gateway'))
    renderOverview()
    // One retry first (a transient failure is asked again once), so allow for its delay.
    expect(await screen.findByRole('alert', {}, { timeout: 4000 })).toBeTruthy()
    expect(screen.getByText(/HTTP 502/)).toBeTruthy()
  })
})

describe('a populated crypto book', () => {
  beforeEach(() => {
    vi.spyOn(api, 'getCryptoPortfolio').mockResolvedValue(book())
    vi.spyOn(api, 'getCryptoHistory').mockResolvedValue(HISTORY)
  })

  it('shows the totals in the base currency with the 24h change', async () => {
    renderOverview()
    const totals = await screen.findByRole('region', { name: 'Crypto totals' })
    expect(within(totals).getByText('Total value')).toBeTruthy()
    expect(within(totals).getByText(/1,000\.00/)).toBeTruthy()
    expect(within(totals).getByText('+1.4%')).toBeTruthy()
    // The base currency comes from /api/settings, like every other figure in the app.
    expect(await within(totals).findByText(/^-CHF\s10\.00$/)).toBeTruthy()
  })

  it('puts every qualifier on the surface, not behind a hover', async () => {
    renderOverview()
    await screen.findByRole('region', { name: 'Crypto totals' })
    expect(screen.getByText(/figures computed by CoinStats/)).toBeTruthy()
    expect(screen.getByText(/USD figures converted at the 2026-09-20 rate/)).toBeTruthy()
    expect(screen.getByText(/cannot price, left out of every total rather than valued at zero: MYST/)).toBeTruthy()
    expect(screen.getAllByText(/Not itemised/).length).toBeGreaterThan(0)
    expect(screen.getByText('3 spam tokens hidden.')).toBeTruthy()
    expect(screen.getByText(/credits are low/)).toBeTruthy()
  })

  it('lists holdings with an unpriced coin as a dash and a fiat balance badged', async () => {
    renderOverview()
    const table = await screen.findByRole('table')
    const rows = within(table).getAllByRole('row')
    const myst = rows.find(r => r.textContent?.includes('MYST'))
    expect(myst?.textContent).toContain('unpriced')
    expect(myst?.textContent).toContain('—')
    expect(myst?.textContent).not.toContain('0.00')
    const eur = rows.find(r => r.textContent?.includes('Euro'))
    expect(eur?.textContent).toContain('cash')
  })

  it('draws the chart and the allocation, with the unitemised share in the legend', async () => {
    renderOverview()
    await waitFor(() => expect(screen.getAllByTestId('chart')).toHaveLength(2))
    const legend = screen.getByRole('list', { name: 'Allocation by coin' })
    expect(within(legend).getByText('10.0%')).toBeTruthy()
    expect(within(legend).getByText(/Not itemised/)).toBeTruthy()
  })
})
