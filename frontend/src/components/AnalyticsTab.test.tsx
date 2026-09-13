// @vitest-environment jsdom
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { render, screen, cleanup, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AnalyticsTab } from './AnalyticsTab'
import { waterfallRows } from '@/lib/returnDecomposition'
import { CurrencyProvider } from '@/lib/CurrencyContext'
import { api } from '@/lib/api'
import type {
  ClosedPositionsResponse,
  DecompositionWindow,
  ReturnDecompositionResponse,
  SegmentAttributionResponse,
} from '@/lib/api'

/**
 * The arithmetic lives on the backend and in `lib/rollingRisk.test.ts`. These cover what only
 * the component can get wrong: that an absent leg renders a dash and never a zero, that every
 * `warnings[]` line is on the surface, that the segment toggle really switches dimension, and
 * that the tab never asks for a benchmark the Performance tab has not selected (a cache miss
 * there is a Yahoo request). Recharts draws nothing in jsdom, so tables and prose are asserted.
 */
afterEach(cleanup)
afterEach(() => vi.restoreAllMocks())
beforeEach(() => {
  if (!('ResizeObserver' in globalThis)) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
  }
})

const window: DecompositionWindow = {
  start_date: '2025-09-12',
  end_date: '2026-09-12',
  start_total_value_eur: 50_000,
  end_total_value_eur: 60_000,
  net_flows_eur: 5_000,
  gain_eur: 5_000,
  gain_pct: 9.52,
  price_effect_eur: 6_000,
  fx_effect_eur: -1_200,
  unsplit_eur: 0,
  unsplit_securities: 0,
  dividends_eur: 300,
  cash_adjustment_eur: null,          // no measured cash balance: absent, not 0
  unexplained_eur: -100,
  unpriced_holdings: 1,
  warnings: ['1 holding could not be priced at an endpoint and is left out of every leg.'],
}

const decomposition: ReturnDecompositionResponse = {
  base_currency: 'CHF',
  cash_source: 'derived',
  window,
  years: [
    { ...window, year: 2025, partial: true, start_date: '2024-12-31', end_date: '2025-12-31', gain_eur: 2_000 },
    { ...window, year: 2026, partial: true, start_date: '2025-12-31', end_date: '2026-09-12', gain_eur: 3_000 },
  ],
}

const segments: SegmentAttributionResponse = {
  start_date: '2025-09-12',
  end_date: '2026-09-12',
  total_pnl_eur: 5_100,
  start_total_value_eur: 50_000,
  end_total_value_eur: 60_000,
  unpriced_holdings: 0,
  basket_as_of_oldest: '2026-09-04',
  proxied_funds: [],
  by_sector: [
    { name: 'Technology', pnl_eur: 4_000, price_effect_eur: 4_500, fx_effect_eur: -500, share_of_gain_pct: 78.4, start_value_eur: 20_000, end_value_eur: 25_000, start_weight_pct: 40, end_weight_pct: 41.7, via_funds_pct: 30 },
    { name: 'Fund residual (cash, derivatives, nested funds)', pnl_eur: 100, price_effect_eur: 100, fx_effect_eur: 0, share_of_gain_pct: 2, start_value_eur: 1_000, end_value_eur: 1_100, start_weight_pct: 2, end_weight_pct: 1.8, via_funds_pct: 100 },
  ],
  by_country: [
    { name: 'United States', pnl_eur: 3_000, price_effect_eur: 3_400, fx_effect_eur: -400, share_of_gain_pct: 58.8, start_value_eur: 30_000, end_value_eur: 34_000, start_weight_pct: 60, end_weight_pct: 56.7, via_funds_pct: 45 },
  ],
  warnings: ['Fund gains are spread by each fund’s current basket.'],
}

const closed: ClosedPositionsResponse = {
  base_currency: 'CHF',
  positions: [
    {
      security_id: 7, symbol: 'VWCE', description: 'Vanguard FTSE All-World', account: 'ibkr',
      still_held: false, lots_closed: 3, first_open_date: '2024-06-01', last_close_date: '2026-08-21',
      holding_days: 700, cost_basis_eur: 20_000, proceeds_eur: 25_000, realized_pnl_eur: 5_000,
      realized_source: 'trade', return_pct: 25, post_sale_pct: null, post_sale_days: null,
    },
  ],
  summary: {
    closed_securities: 1, winners: 1, losers: 0, hit_rate_pct: 100, total_realized_eur: 5_000,
    total_cost_eur: 20_000, avg_holding_days: 700, best: 'VWCE', worst: null,
    post_sale_judged: 0, sold_then_rose: 0, sold_then_fell: 0,
  },
  warnings: ['No sold position has a price after its last sale on record.'],
}

function renderTab(benchmark: { key: string; name: string } | null = null) {
  vi.spyOn(api, 'getReturnDecomposition').mockResolvedValue(decomposition)
  vi.spyOn(api, 'getSegmentAttribution').mockResolvedValue(segments)
  vi.spyOn(api, 'getClosedPositions').mockResolvedValue(closed)
  vi.spyOn(api, 'getPortfolioValueOverTime').mockResolvedValue([])
  const benchmarkSpy = vi.spyOn(api, 'getBenchmarkComparison').mockResolvedValue({
    benchmark_name: 'S&P 500', benchmark_ticker: '^GSPC', data: [],
  })
  vi.spyOn(api, 'getSettings').mockResolvedValue({ base_currency: 'CHF', supported_currencies: ['EUR', 'CHF', 'USD'] } as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <CurrencyProvider>
        <AnalyticsTab inception="2024-05-28" benchmark={benchmark} />
      </CurrencyProvider>
    </QueryClientProvider>,
  )
  return { benchmarkSpy }
}

describe('AnalyticsTab', () => {
  it('renders the legs, dashes the absent one, and keeps every caveat on the surface', async () => {
    renderTab()
    const gain = await screen.findByText('Gain', { selector: 'p, span, div, h3, dt' })
    expect(gain).toBeTruthy()
    // The cash adjustment is null: a dash, never CHF 0.00.
    const fees = await screen.findByText('Cash adjustment', { selector: 'p, span, div, h3, dt' })
    const feesCard = fees.closest('[class*="rounded"]') ?? fees.parentElement!
    expect(within(feesCard as HTMLElement).getByText('—')).toBeTruthy()
    expect(within(feesCard as HTMLElement).queryByText(/0\.00/)).toBeNull()
    // Caveats collapse to one line, but the material qualifier stays on that line.
    const summary = await screen.findByText(/1 note · 1 unpriced holding left out/)
    expect(summary.closest('details')).toBeTruthy()
    expect(screen.getByText(/could not be priced at an endpoint/)).toBeTruthy()
    // The year table carries both years, marked partial.
    expect(await screen.findByText('2025 (partial)')).toBeTruthy()
    expect(screen.getByText('2026 (partial)')).toBeTruthy()
  })

  it('switches the segment table from sectors to countries', async () => {
    renderTab()
    expect(await screen.findByText('Technology')).toBeTruthy()
    expect(screen.getByText('Fund residual (cash, derivatives, nested funds)')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Country' }))
    expect(await screen.findByText('United States')).toBeTruthy()
    expect(screen.queryByText('Technology')).toBeNull()
  })

  it('never asks for a benchmark the Performance tab has not selected', async () => {
    const { benchmarkSpy } = renderTab(null)
    await screen.findByText('Technology')
    expect(benchmarkSpy).not.toHaveBeenCalled()
    expect(screen.getByText(/Pick a benchmark on the Performance tab/)).toBeTruthy()
  })

  it('asks for the selected benchmark with the same range', async () => {
    const { benchmarkSpy } = renderTab({ key: 'sp500', name: 'S&P 500' })
    await screen.findByText('Technology')
    expect(benchmarkSpy).toHaveBeenCalledTimes(1)
    expect(benchmarkSpy.mock.calls[0][2]).toBe('sp500')
  })

  it('shows the closed position with its source and an absent post-sale move', async () => {
    renderTab()
    expect(await screen.findByText('VWCE')).toBeTruthy()
    expect(screen.getByText('IBKR')).toBeTruthy()
    expect(screen.getByText('1 winners · 0 losers')).toBeTruthy()
    expect(screen.getByText('No price after any sale on record')).toBeTruthy()
  })
})

describe('waterfallRows', () => {
  it('walks start → legs → end so the last bar lands exactly on the end value', () => {
    const rows = waterfallRows(window)
    expect(rows[0]).toMatchObject({ name: 'Start', value: 50_000, kind: 'total' })
    expect(rows[rows.length - 1]).toMatchObject({ name: 'End', value: 60_000, kind: 'total' })
    // Null and zero legs are skipped, not drawn as zero-height bars claiming a figure.
    expect(rows.map((r) => r.name)).toEqual(['Start', 'Paid in', 'Price', 'FX', 'Dividends', 'Unexplained', 'End'])
    // Running total after every leg reaches the end value.
    const walked = rows.slice(1, -1).reduce((acc, r) => acc + r.value, window.start_total_value_eur!)
    expect(walked).toBeCloseTo(window.end_total_value_eur!, 6)
    // A loss bar sits on the lower of its two ends.
    const fx = rows.find((r) => r.name === 'FX')!
    expect(fx.kind).toBe('loss')
    expect(fx.base).toBeCloseTo(50_000 + 5_000 + 6_000 - 1_200, 6)
    expect(fx.size).toBe(1_200)
  })

  it('is empty when either endpoint is unknown', () => {
    expect(waterfallRows({ ...window, start_total_value_eur: null })).toEqual([])
  })
})
