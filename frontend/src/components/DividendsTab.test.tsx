// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api, type DividendBreakdownResponse } from '@/lib/api'
import { DividendsTab } from './DividendsTab'

// jsdom cannot measure charts. Keep all controls, data transforms and captions real.
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
})

function response(year: number | undefined, period?: '24m'): DividendBreakdownResponse {
  return {
    years: [2025, 2026, 2027], year: year ?? null, period: period ?? null,
    months: [
      { month: '2026-01', actual: { AAA: 30 }, forecast: {}, actual_total_eur: 30,
        forecast_total_eur: 0, ttm_net_eur: 120, ttm_mom_pct: 10, ttm_source: 'mixed',
        ttm_mom_crosses_era: true },
      { month: '2026-02', actual: {}, forecast: { AAA: 50 }, actual_total_eur: 0,
        forecast_total_eur: 50, ttm_net_eur: null, ttm_mom_pct: null },
    ],
    total_net_eur: period === '24m' ? 200 : 30, total_forecast_net_eur: 50,
    securities: [], ibkr_from: '2025-06-01', base_currency: 'EUR',
    growth: {
      ttm: { net_eur: 130, prev_net_eur: 100, pct: 30 },
      ytd: { net_eur: 30, prev_net_eur: 20, pct: 50 },
      avg_month: { net_eur: 10, prev_net_eur: 8, pct: 25 },
      ttm_crosses_era: true, next_12m_eur: 160, next_12m_vs_ttm_pct: 23.1,
      latest_month: null,
      annual: [{ year: 2025, net_eur: 100, forecast_net_eur: 0, total_eur: 100,
        yoy_pct: null, yoy_includes_forecast: false, yoy_vs_partial: false, partial: false }],
    },
  }
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  return render(<QueryClientProvider client={client}><DividendsTab /></QueryClientProvider>)
}

describe('Dividend chart controls', () => {
  it('switches chart modes and forecasts without refetching or changing historical TTM', async () => {
    const request = vi.spyOn(api, 'getDividendBreakdown').mockImplementation(async (year, period) => response(year, period))
    const user = userEvent.setup()
    mount()
    await screen.findByText(/Received/)
    expect(screen.getByRole('button', { name: 'Monthly' }).getAttribute('aria-pressed')).toBe('true')
    await user.click(screen.getByRole('button', { name: 'TTM' }))
    const latest = screen.getByLabelText('Latest completed TTM')
    expect(latest.textContent).toContain('120.00')
    expect(latest.textContent).toContain('Feb 25 – Jan 26')
    expect(latest.textContent).toContain('+10%')
    expect(screen.getByText(/Earlier history includes estimated gross/)).toBeTruthy()
    expect(screen.getByText(/Some TTM comparisons span the switch/)).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Toggle forecast overlay' }))
    expect(screen.queryByText(/projected \+/)).toBeNull()
    expect(latest.textContent).toContain('120.00')
    expect(request).toHaveBeenCalledTimes(1)
  })

  it('requests 24 months and allows an annual row to restore a calendar year without changing mode', async () => {
    const request = vi.spyOn(api, 'getDividendBreakdown').mockImplementation(async (year, period) => response(year, period))
    const user = userEvent.setup()
    mount()
    await screen.findByText(/Received/)
    await user.click(screen.getByRole('button', { name: 'TTM' }))
    await user.selectOptions(screen.getByRole('combobox', { name: 'Dividend period' }), '24m')
    await screen.findByText(/Received.*200/)
    expect(request).toHaveBeenLastCalledWith(undefined, '24m')
    expect(screen.getByRole('button', { name: 'TTM' }).getAttribute('aria-pressed')).toBe('true')
    const annual = screen.getByText('Per year').parentElement!
    await user.click(within(annual).getByRole('button'))
    await screen.findByText(/Received.*30/)
    expect(request).toHaveBeenLastCalledWith(2025, undefined)
    expect(screen.getByRole('button', { name: 'TTM' }).getAttribute('aria-pressed')).toBe('true')
  })

  it('shows unavailable TTM rather than zero for short or future history', async () => {
    const data = response(2027)
    data.months = data.months.map(m => ({ ...m, ttm_net_eur: null }))
    data.total_net_eur = 0
    data.total_forecast_net_eur = 0
    vi.spyOn(api, 'getDividendBreakdown').mockResolvedValue(data)
    const user = userEvent.setup()
    mount()
    await screen.findByText(/No dividends recorded/)
    await user.click(screen.getByRole('button', { name: 'TTM' }))
    expect(screen.getByText(/No completed 12-month window/)).toBeTruthy()
    expect(screen.queryByLabelText('Latest completed TTM')).toBeNull()
  })

  it('still displays trailing income when the selected range received no payments', async () => {
    const data = response(2026)
    data.total_net_eur = 0
    data.total_forecast_net_eur = 0
    vi.spyOn(api, 'getDividendBreakdown').mockResolvedValue(data)
    const user = userEvent.setup()
    mount()
    await screen.findByText(/No dividends recorded/)
    await user.click(screen.getByRole('button', { name: 'TTM' }))
    expect(screen.getByLabelText('Latest completed TTM').textContent).toContain('120.00')
  })

  it('keeps an API failure distinct from an empty history', async () => {
    vi.spyOn(api, 'getDividendBreakdown').mockRejectedValue(new Error('offline'))
    mount()
    await screen.findByText(/Couldn't load dividends/)
    expect(screen.queryByText(/No dividends recorded/)).toBeNull()
  })

  it('does not show the old TTM or an empty-history message while a new range loads', async () => {
    vi.spyOn(api, 'getDividendBreakdown')
      .mockResolvedValueOnce(response(2026))
      .mockImplementationOnce(() => new Promise(() => {}))
    const user = userEvent.setup()
    mount()
    await screen.findByText(/Received/)
    await user.click(screen.getByRole('button', { name: 'TTM' }))
    expect(screen.getByLabelText('Latest completed TTM')).toBeTruthy()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Dividend period' }), '24m')
    expect(screen.queryByLabelText('Latest completed TTM')).toBeNull()
    expect(screen.queryByText(/No completed 12-month window/)).toBeNull()
  })

  it('shows a measured zero TTM and its decline instead of hiding the point', async () => {
    const data = response(2026)
    data.months[0].ttm_net_eur = 0
    data.months[0].ttm_mom_pct = -100
    data.months[0].ttm_source = null
    data.total_net_eur = 0
    data.total_forecast_net_eur = 0
    vi.spyOn(api, 'getDividendBreakdown').mockResolvedValue(data)
    const user = userEvent.setup()
    mount()
    await screen.findByText(/No dividends recorded/)
    await user.click(screen.getByRole('button', { name: 'TTM' }))
    const latest = screen.getByLabelText('Latest completed TTM')
    expect(latest.textContent).toContain('€0.00')
    expect(latest.textContent).toContain('100%')
    expect(screen.queryByText(/No completed 12-month window/)).toBeNull()
  })
})
