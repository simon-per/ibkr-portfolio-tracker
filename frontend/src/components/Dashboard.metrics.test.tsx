// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Dashboard } from './Dashboard'
import { api } from '@/lib/api'

/**
 * Review item M2: the Returns and Risk rows were gated on two points of the series, so on
 * the first day of an MTD or YTD both vanished — Win rate, Top 5 and the dividend figures
 * with them, though none of those reads the series.
 */

vi.mock('./PortfolioValueChart', () => ({ PortfolioValueChart: () => null }))

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

beforeEach(() => {
  localStorage.clear()
  // Every read not answered below stays pending.
  vi.stubGlobal('fetch', () => new Promise(() => {}))
})

const position = (symbol: string, mv: number, cost: number) => ({
  security_id: symbol.length, symbol, description: symbol, isin: `US000000000${symbol.length}`,
  quantity: 10, market_price: mv / 10, market_value_eur: mv, cost_basis_eur: cost,
  gain_loss_eur: mv - cost, gain_loss_percent: ((mv - cost) / cost) * 100,
  currency: 'USD', asset_category: 'STK',
})

describe('Dashboard metrics rows', () => {
  it('render on a one-point series, the series figures dashed', async () => {
    vi.spyOn(api, 'getPortfolioValueOverTime').mockResolvedValue([
      {
        date: '2026-11-02', market_value_eur: 2000, cost_basis_eur: 1800,
        gain_loss_eur: 200, gain_loss_percent: 11.1, unpriced_holdings: 0,
      },
    ] as unknown as Awaited<ReturnType<typeof api.getPortfolioValueOverTime>>)
    vi.spyOn(api, 'getPositions').mockResolvedValue(
      [position('AAA', 1200, 1000), position('BBBB', 800, 800)] as unknown as Awaited<
        ReturnType<typeof api.getPositions>
      >,
    )
    vi.spyOn(api, 'getAnnualizedReturn').mockResolvedValue({
      annualized_return_pct: null, method: 'period', unpriced_holdings: 0,
    } as unknown as Awaited<ReturnType<typeof api.getAnnualizedReturn>>)

    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <Dashboard />
      </QueryClientProvider>,
    )

    expect(await screen.findByText('Win rate')).toBeTruthy()
    expect(await screen.findByText('Volatility')).toBeTruthy()
    // Win rate does not read the series: one of the two judged holdings is at a gain.
    expect(screen.getByText('50.0%')).toBeTruthy()
  })
})
