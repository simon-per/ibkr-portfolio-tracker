// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  api,
  type DividendForecastHistoryPoint,
  type DividendForecastHistoryResponse,
} from '@/lib/api'
import { DividendForecastTrend } from './DividendForecastTrend'

// jsdom cannot measure charts. Everything around the plot stays real.
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

function point(
  date: string,
  value: number,
  extra: Partial<DividendForecastHistoryPoint> = {},
): DividendForecastHistoryPoint {
  return {
    date, next_12m_eur: value, ttm_net_eur: 50,
    next_12m_change_pct: null, next_12m_mtd_pct: null, next_12m_ytd_pct: null,
    mtd_from: null, ytd_from: null, next_12m_since_start_pct: null, breakdown: null,
    reconstructed: false, ...extra,
  }
}

const HISTORY: DividendForecastHistoryResponse = {
  base_currency: 'EUR',
  growth_from: '2026-08-28',
  points: [
    point('2026-08-01', 40, { reconstructed: true }),
    point('2026-08-28', 300, { reconstructed: true }),
    point('2026-10-13', 360, {
      next_12m_since_start_pct: 20,
      breakdown: {
        since: '2026-08-28', shares: 52, payout: 9, newly_forecast: 0, fx: -1,
        unattributed: 0,
      },
    }),
  ],
}

function mount(history: DividendForecastHistoryResponse) {
  vi.spyOn(api, 'getDividendForecastHistory').mockResolvedValue(history)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  return render(
    <QueryClientProvider client={client}>
      <DividendForecastTrend />
    </QueryClientProvider>,
  )
}

describe('DividendForecastTrend', () => {
  it('starts closed with the growth on the surface, and opens to the chart and its parts', async () => {
    mount(HISTORY)

    const section = await screen.findByRole('region', { name: 'Dividend forecast over time' })
    // Collapsed: the figure, the growth since the start day and its "est." stay visible.
    expect(section.textContent).toContain('since Aug 28')
    expect(section.textContent).toContain('+20%')
    expect(section.textContent).toContain('est.')
    expect(screen.queryByTestId('chart')).toBeNull()
    expect(section.textContent).not.toContain('Of the change')

    const toggle = screen.getByRole('button', { expanded: false })
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(document.getElementById(toggle.getAttribute('aria-controls')!)).not.toBeNull()
    expect(screen.getByTestId('chart')).toBeTruthy()
    // The parts that round to nothing are not printed, the residual among them.
    expect(section.textContent).toMatch(/Of the change:.*shares bought.*payouts and timing.*currency/)
    expect(section.textContent).not.toContain('newly forecast')
    expect(section.textContent).not.toContain('unexplained')

    fireEvent.click(toggle)
    expect(screen.queryByTestId('chart')).toBeNull()
  })

  it('renders nothing until there are two days to connect', async () => {
    const spy = vi.spyOn(api, 'getDividendForecastHistory')
    mount({ ...HISTORY, points: [point('2026-10-13', 360)] })
    await vi.waitFor(() => expect(spy).toHaveBeenCalled())
    await Promise.resolve()
    expect(screen.queryByRole('region')).toBeNull()
  })
})
