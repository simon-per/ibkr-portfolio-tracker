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
        forecast_total_eur: 0 },
      { month: '2026-02', actual: {}, forecast: { AAA: 50 }, actual_total_eur: 0,
        forecast_total_eur: 50 },
    ],
    // One closed window and one open, projected one — the two states the Forecast
    // toggle has to tell apart.
    ttm_series: [
      { month: '2026-01', actual: { AAA: 120 }, forecast: {}, net_eur: 120,
        forecast_net_eur: 0, total_eur: 120, mom_pct: 10, mom_includes_forecast: false,
        source: 'mixed', mom_crosses_era: true, partial: false },
      { month: '2026-02', actual: { AAA: 100 }, forecast: { AAA: 50 }, net_eur: 100,
        forecast_net_eur: 50, total_eur: 150, mom_pct: 25, mom_includes_forecast: true,
        source: 'mixed', mom_crosses_era: false, partial: true },
    ],
    // The earliest window on record. Equal to the first ttm_series point here, so
    // the fixture exercises the coverage-limited marker.
    ttm_coverage_start: '2026-01',
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
  it('switches chart modes and forecasts without refetching or moving a closed window', async () => {
    const request = vi.spyOn(api, 'getDividendBreakdown').mockImplementation(async (year, period) => response(year, period))
    const user = userEvent.setup()
    mount()
    await screen.findByText(/Received/)
    expect(screen.getByRole('button', { name: 'Monthly' }).getAttribute('aria-pressed')).toBe('true')
    await user.click(screen.getByRole('button', { name: 'TTM' }))
    // Forecast on: the newest window is the open one, quoted as its full total and
    // saying so — the amount carries its own marker, not just the delta chip.
    const open = screen.getByLabelText('Latest rolling 12 months')
    expect(open.textContent).toContain('150.00')
    expect(open.textContent).toContain('Mar 25 – Feb 26')
    expect(open.textContent).toContain('incl. €50.00 projected')
    expect(open.textContent).toContain('est.')
    expect(screen.getByText(/Earlier history includes estimated gross/)).toBeTruthy()
    expect(screen.getByText(/Some comparisons span the switch/)).toBeTruthy()

    await user.click(screen.getByRole('button', { name: 'Toggle forecast overlay' }))
    // Forecast off: the open window goes, leaving the last fully elapsed one —
    // which is exactly what this chart drew before projections were folded in.
    const closed = screen.getByLabelText('Latest rolling 12 months')
    expect(closed.textContent).toContain('120.00')
    expect(closed.textContent).toContain('Feb 25 – Jan 26')
    expect(closed.textContent).toContain('+10%')
    expect(closed.textContent).not.toContain('projected')
    expect(screen.queryByText(/projected \+/)).toBeNull()
    expect(request).toHaveBeenCalledTimes(1)
  })

  it('measures growth across the windows on screen, and follows the toggle', async () => {
    // The bug this replaced: the rate was anchored at the projection horizon and
    // read the same whatever the reader selected. Hiding projections drops the
    // open window, so the span shortens and the figure must move with it —
    // without refetching, since both windows are already on the response.
    const request = vi.spyOn(api, 'getDividendBreakdown').mockResolvedValue(response(2026))
    const user = userEvent.setup()
    mount()
    await screen.findByText(/Received/)

    const withForecast = screen.getByLabelText('Dividend income growth').textContent!
    expect(withForecast).toContain('Jan 26')
    expect(withForecast).toContain('Feb 26')   // the open window is the endpoint
    expect(withForecast).toContain('est.')

    await user.click(screen.getByRole('button', { name: 'Toggle forecast overlay' }))
    // One closed window left, so there is no span to measure and no figure.
    expect(screen.queryByLabelText('Dividend income growth')).toBeNull()
    expect(request).toHaveBeenCalledTimes(1)
  })

  it('keeps the growth figure on screen in both chart modes', async () => {
    // It describes the income over the selected range, not one view of it.
    vi.spyOn(api, 'getDividendBreakdown').mockResolvedValue(response(2026))
    const user = userEvent.setup()
    mount()
    await screen.findByText(/Received/)
    expect(screen.getByLabelText('Dividend income growth')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'TTM' }))
    expect(screen.getByLabelText('Dividend income growth')).toBeTruthy()
  })

  it('shows the per-symbol key and the forecast key in TTM mode, not only monthly', async () => {
    // Both views are the same stack in the same colours, so a legend that appears
    // for one and not the other leaves segments on screen with nothing naming them.
    vi.spyOn(api, 'getDividendBreakdown').mockResolvedValue(response(2026))
    const user = userEvent.setup()
    mount()
    await screen.findByText(/Received/)
    await user.click(screen.getByRole('button', { name: 'TTM' }))
    expect(screen.getByText('AAA')).toBeTruthy()
    expect(screen.getByText('translucent = forecast')).toBeTruthy()
  })

  it('tells the reader which windows it is showing, and changes that with the toggle', async () => {
    vi.spyOn(api, 'getDividendBreakdown').mockResolvedValue(response(2026))
    const user = userEvent.setup()
    mount()
    await screen.findByText(/Received/)
    await user.click(screen.getByRole('button', { name: 'TTM' }))
    expect(screen.getByText(/include projected payments/)).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Toggle forecast overlay' }))
    expect(screen.getByText(/Only fully elapsed windows are shown/)).toBeTruthy()
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
    data.ttm_series = []
    data.total_net_eur = 0
    data.total_forecast_net_eur = 0
    vi.spyOn(api, 'getDividendBreakdown').mockResolvedValue(data)
    const user = userEvent.setup()
    mount()
    await screen.findByText(/No dividends recorded/)
    await user.click(screen.getByRole('button', { name: 'TTM' }))
    expect(screen.getByText(/No twelve-month window is covered/)).toBeTruthy()
    expect(screen.queryByLabelText('Latest rolling 12 months')).toBeNull()
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
    // The rolling total is unwindowed, so it still has a figure when the selected
    // range itself received nothing — which is when the trend is most worth seeing.
    expect(screen.getByLabelText('Latest rolling 12 months').textContent).toContain('150.00')
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
    expect(screen.getByLabelText('Latest rolling 12 months')).toBeTruthy()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Dividend period' }), '24m')
    expect(screen.queryByLabelText('Latest rolling 12 months')).toBeNull()
    expect(screen.queryByText(/No twelve-month window is covered/)).toBeNull()
  })

  it('shows a measured zero TTM and its decline instead of hiding the point', async () => {
    const data = response(2026)
    data.ttm_series = [{ ...data.ttm_series[0], actual: {}, net_eur: 0, total_eur: 0,
      mom_pct: -100, source: null, mom_crosses_era: false }]
    data.total_net_eur = 0
    data.total_forecast_net_eur = 0
    vi.spyOn(api, 'getDividendBreakdown').mockResolvedValue(data)
    const user = userEvent.setup()
    mount()
    await screen.findByText(/No dividends recorded/)
    await user.click(screen.getByRole('button', { name: 'TTM' }))
    const latest = screen.getByLabelText('Latest rolling 12 months')
    expect(latest.textContent).toContain('€0.00')
    expect(latest.textContent).toContain('100%')
    expect(screen.queryByText(/No twelve-month window is covered/)).toBeNull()
  })
})
