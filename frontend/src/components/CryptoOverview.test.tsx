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
    configured: true, prices_configured: true, base_currency: 'CHF',
    as_of: '2026-09-20T12:00:00+00:00', prices_as_of: '2026-09-20T12:00:03+00:00',
    total_value: 1000, defi_value: null, cash_value: 400, change_today: -10,
    change_today_pct: 1.35, pnl_since_start: 190, start_date: '2026-01-01',
    basket_date: '2026-08-23', first_snapshot_date: '2026-09-15',
    peg_note: 'Valued at a fixed 1.00 USD peg where CoinGecko has no price — USDC on 2 days. A deliberate exception, not a market price.',
    fx_unavailable: 0, valued_count: 2, spam_count: 3, unpriced_count: 1,
    unpriced_symbols: ['MYST'], no_price_symbols: [],
    holdings: [
      {
        coin_id: 'bitcoin', symbol: 'BTC', name: 'Bitcoin', rank: 1, status: 'valued',
        quantity: 0.01, price: 50_000, price_source: 'coingecko', value: 500,
        weight_pct: 50, change_today_pct: 2,
      },
      {
        coin_id: 'usd-coin', symbol: 'USDC', name: 'USDC', rank: 7, status: 'valued',
        quantity: 500, price: 1, price_source: 'peg', value: 500, weight_pct: 50,
        change_today_pct: 0,
      },
      {
        coin_id: 'mystery-token', symbol: 'MYST', name: 'Mystery', rank: null,
        status: 'unpriced', quantity: 42, price: null, price_source: null, value: null,
        weight_pct: null, change_today_pct: null,
      },
    ],
    color_order: ['bitcoin', 'usd-coin'],
    warnings: ['CoinStats credits are low: 3000 of 20000 left this period.'],
    ...over,
  }
}

const HISTORY: CryptoHistoryResponse = {
  configured: true, prices_configured: true, base_currency: 'CHF',
  fetched_at: '2026-09-20T06:00:00+00:00', start_date: '2026-01-01',
  basket_date: '2026-08-23', first_snapshot_date: '2026-09-19', peg_note: null,
  fx_unavailable: 0, warnings: [],
  points: [
    { date: '2026-09-18', value: 950, pnl: 15, reconstructed: true },
    { date: '2026-09-19', value: null, pnl: null, reconstructed: false },
    { date: '2026-09-20', value: 1000, pnl: 19, reconstructed: false },
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

  it('shows the total, today and the P&L since 1 January in the base currency', async () => {
    renderOverview()
    const totals = await screen.findByRole('region', { name: 'Crypto totals' })
    expect(within(totals).getByText('Total value')).toBeTruthy()
    expect(within(totals).getByText(/1,000\.00/)).toBeTruthy()
    expect(within(totals).getByText('Today')).toBeTruthy()
    expect(within(totals).getByText('+1.4%')).toBeTruthy()
    expect(within(totals).getByText(/^P&L since Jan 1, 2026$/)).toBeTruthy()
    // The base currency comes from /api/settings, like every other figure in the app.
    expect(await within(totals).findByText(/^-CHF\s10\.00$/)).toBeTruthy()
    expect(within(totals).getByText(/^\+CHF\s190\.00$/)).toBeTruthy()
    // CoinStats' cost and P&L tiles are gone.
    expect(within(totals).queryByText('Cost basis')).toBeNull()
    expect(within(totals).queryByText('Unrealized P&L')).toBeNull()
  })

  it('puts every qualifier on the surface, not behind a hover', async () => {
    renderOverview()
    await screen.findByRole('region', { name: 'Crypto totals' })
    expect(screen.getByText(/prices from CoinGecko/)).toBeTruthy()
    expect(screen.getByText(/in exchange cash, not in this total/)).toBeTruthy()
    expect(screen.getByText(/the Aug 23, 2026 coins at each day's price/)).toBeTruthy()
    expect(screen.getByText(/rebuilt from CoinStats' transactions/)).toBeTruthy()
    expect(screen.getByText(/fixed 1.00 USD peg/)).toBeTruthy()
    expect(screen.getByText(/cannot price, left out of every total rather than valued at zero: MYST/)).toBeTruthy()
    expect(screen.getByText('3 spam tokens hidden.')).toBeTruthy()
    expect(screen.getByText(/credits are low/)).toBeTruthy()
  })

  it('says why the total is unknown when a coin has no price', async () => {
    vi.spyOn(api, 'getCryptoPortfolio').mockResolvedValue(book({
      total_value: null, no_price_symbols: ['ETH'],
    }))
    renderOverview()
    const totals = await screen.findByRole('region', { name: 'Crypto totals' })
    expect(within(totals).getByText('Unknown: no price for ETH')).toBeTruthy()
  })

  it('names a coin left out of a known total, beside the figure', async () => {
    vi.spyOn(api, 'getCryptoPortfolio').mockResolvedValue(book({
      total_value: 1200, no_price_symbols: ['BNB'],
    }))
    renderOverview()
    const totals = await screen.findByRole('region', { name: 'Crypto totals' })
    expect(within(totals).getByText('Excludes BNB — no price')).toBeTruthy()
  })

  it('lists holdings with an unpriced coin as a dash and a pegged price badged', async () => {
    renderOverview()
    const table = await screen.findByRole('table')
    const rows = within(table).getAllByRole('row')
    const myst = rows.find(r => r.textContent?.includes('MYST'))
    expect(myst?.textContent).toContain('unpriced')
    expect(myst?.textContent).toContain('—')
    expect(myst?.textContent).not.toContain('0.00')
    const usdc = rows.find(r => r.textContent?.includes('USDC'))
    expect(usdc?.textContent).toContain('peg')
  })

  it('draws the chart and the allocation, with every share in the legend', async () => {
    renderOverview()
    await waitFor(() => expect(screen.getAllByTestId('chart')).toHaveLength(2))
    const legend = screen.getByRole('list', { name: 'Allocation by coin' })
    expect(within(legend).getAllByText('50.0%')).toHaveLength(2)
  })
})
