// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  api, UnauthorizedError, type CryptoPortfolioResponse, type CryptoStatusResponse,
} from '@/lib/api'
import { CurrencyProvider } from '@/lib/CurrencyContext'
import { ModeProvider } from '@/lib/portfolioMode'
import { CryptoDashboard, CryptoStatusLine, CryptoSyncMessage } from './CryptoDashboard'

vi.mock('recharts', async () => {
  const actual = await vi.importActual<typeof import('recharts')>('recharts')
  return { ...actual, ResponsiveContainer: () => <div data-testid="chart" /> }
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  localStorage.clear()
})

const STATUS: CryptoStatusResponse = {
  configured: true,
  last_run: {
    status: 'success', reason: null, message: 'Crypto snapshot stored',
    finished_at: '2026-09-20T12:00:05+00:00',
  },
  last_snapshot_at: '2026-09-20T12:00:00+00:00',
  next_run: '2026-09-20T15:00:00+02:00',
  sync_in_progress: false,
  manual_retry_after_seconds: 0,
  credits_remaining: 19_900,
  credits_total: 20_000,
  credits_plan: 'free',
  credits_spent_last_run: 18,
}

/** Configured, never synced: the overview shows its first-sync prompt. */
const EMPTY_BOOK: CryptoPortfolioResponse = {
  configured: true, base_currency: 'EUR', as_of: null, total_value: null,
  itemised_value: null, unitemised_value: null, defi_value: null, total_cost: null,
  unrealized_pl: null, unrealized_pl_pct: null, realized_pl: null, realized_pl_pct: null,
  all_time_pl: null, all_time_pl_pct: null, change_24h: null, change_24h_pct: null,
  fx_caveat: null, fx_unavailable: 0, valued_count: 0, spam_count: 0, unpriced_count: 0,
  unpriced_symbols: [], holdings: [], color_order: [], warnings: [],
}

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  })
  vi.spyOn(api, 'getSettings').mockResolvedValue({
    base_currency: 'EUR', supported_currencies: ['EUR', 'CHF', 'USD'],
    dividend_forecast_withholding_pct: 15,
  })
  vi.spyOn(api, 'healthCheck').mockResolvedValue({
    status: 'healthy', version: '1.0.0', commit: 'abcdef0', scheduler_enabled: true,
    write_auth_enabled: true,
  })
})

function renderDashboard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <CurrencyProvider>
        <ModeProvider>
          <CryptoDashboard />
        </ModeProvider>
      </CurrencyProvider>
    </QueryClientProvider>,
  )
}

describe('CryptoDashboard', () => {
  it('shares the header: the mode toggle and theme toggle sit beside a crypto Sync button', async () => {
    vi.spyOn(api, 'getCryptoStatus').mockResolvedValue(STATUS)
    vi.spyOn(api, 'getCryptoPortfolio').mockResolvedValue(EMPTY_BOOK)
    renderDashboard()
    expect(screen.getByRole('group', { name: 'Portfolio' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Toggle theme' })).toBeTruthy()
    expect(await screen.findByText(/CoinStats credits: 19,900 of 20,000 left/)).toBeTruthy()
    expect(screen.getByText(/Last sync:/)).toBeTruthy()
  })

  it('runs a sync and reports it under the header', async () => {
    vi.spyOn(api, 'getCryptoStatus').mockResolvedValue(STATUS)
    vi.spyOn(api, 'getCryptoPortfolio').mockResolvedValue(EMPTY_BOOK)
    const sync = vi.spyOn(api, 'syncCrypto').mockResolvedValue({
      type: 'crypto_sync', status: 'success', reason: null,
      message: 'Crypto snapshot stored; history refreshed', warnings: ['a caveat'],
      timestamp: '2026-09-20T12:00:05+00:00',
    })
    renderDashboard()
    const button = await screen.findByRole('button', { name: 'Sync crypto' })
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(button)
    expect(await screen.findByText(/Crypto snapshot stored; history refreshed/)).toBeTruthy()
    expect(screen.getByText(/a caveat/)).toBeTruthy()
    expect(sync).toHaveBeenCalledTimes(1)
  })

  it('disables the Sync button and says so while crypto is locked', async () => {
    vi.spyOn(api, 'getCryptoStatus').mockRejectedValue(new UnauthorizedError('x'))
    vi.spyOn(api, 'getCryptoPortfolio').mockRejectedValue(new UnauthorizedError('x'))
    renderDashboard()
    expect(await screen.findByText(/Locked — add the admin key/)).toBeTruthy()
    const button = screen.getByRole('button', { name: 'Sync crypto' }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
  })
})

describe('CryptoStatusLine', () => {
  it('shows what went wrong on a failed run rather than a bare status', () => {
    render(
      <CryptoStatusLine
        access="open"
        status={{ ...STATUS, last_run: { ...STATUS.last_run!, status: 'error', message: 'CoinStats rejected the share token' } }}
      />,
    )
    expect(screen.getByText('CoinStats rejected the share token')).toBeTruthy()
  })
})

describe('CryptoSyncMessage', () => {
  it('keeps a skip neutral and an error red', () => {
    const { rerender, container } = render(
      <CryptoSyncMessage data={{ type: 'crypto_sync', status: 'skipped', reason: 'not_synced', message: 'CoinStats is still syncing', warnings: [], timestamp: null }} />,
    )
    expect(screen.getByText(/⏸ CoinStats is still syncing/)).toBeTruthy()
    expect(container.innerHTML).not.toContain('bg-red-50')
    rerender(<CryptoSyncMessage error={{ message: 'HTTP 429' }} />)
    expect(screen.getByText(/Crypto sync failed: HTTP 429/)).toBeTruthy()
  })
})
