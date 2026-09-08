// @vitest-environment jsdom
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { render, screen, cleanup, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ForecastTab } from './ForecastTab'
import { CurrencyProvider } from '@/lib/CurrencyContext'
import { api } from '@/lib/api'
import type { ContributionsResponse, PortfolioSummary } from '@/lib/api'

/**
 * The arithmetic is pinned in `lib/forecast.test.ts`. These cover what only the component
 * can get wrong: that the table and the sentence under the chart read the money-in
 * baseline rather than the market value, that a baseline which failed to load is refused
 * on screen rather than drawn as zero, and that the Current button reports what the account
 * is worth today — Total Value when cash is tracked — whichever starting point is selected.
 *
 * Recharts renders nothing inside jsdom's zero-size container, so the chart's legend and
 * bands are invisible here by construction. The table and the prose are what is asserted.
 */

afterEach(cleanup)
afterEach(() => vi.restoreAllMocks())
beforeEach(() => localStorage.clear())

/** `ScrollableTable` measures overflow with a ResizeObserver, which jsdom does not implement. */
beforeEach(() => {
  if (!('ResizeObserver' in globalThis)) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
  }
})

/** Holdings 60,000 plus 12,000 of cash, against 55,000 paid in: 17,000 of gain already made. */
const summary: PortfolioSummary = {
  total_cost_basis_eur: 50_000,
  total_market_value_eur: 60_000,
  total_gain_loss_eur: 10_000,
  total_gain_loss_percent: 20,
  num_positions: 3,
  total_cash_eur: 12_000,
  total_value_eur: 72_000,
  cash_source: 'derived',
  total_realized_gain_loss_eur: 0,
  total_realized_proceeds_eur: 0,
  total_realized_cost_basis_eur: 0,
  num_closed_positions: 0,
}

const contributions: ContributionsResponse = {
  windows: [
    {
      label: 'all',
      months: 24,
      partial: false,
      money_in_eur: 55_000,
      avg_money_in_per_month_eur: 55_000 / 24,
      money_in_method: 'spliced',
      deposits_eur: 30_000,
      deployed_eur: 60_000,
      avg_deployed_per_month_eur: 2_500,
      net_eur: 50_000,
    },
  ],
  monthly: [],
  first_contribution_date: '2024-05-28',
  coverage_from: '2026-01-09',
  deposits_from: '2026-01-09',
  transfer_in_date: null,
  base_currency: 'CHF',
}

function mockApi(over: { summary?: PortfolioSummary; contributions?: ContributionsResponse | Error } = {}) {
  vi.spyOn(api, 'getPortfolioSummary').mockResolvedValue(over.summary ?? summary)
  const c = over.contributions ?? contributions
  if (c instanceof Error) vi.spyOn(api, 'getContributions').mockRejectedValue(c)
  else vi.spyOn(api, 'getContributions').mockResolvedValue(c)
}

/** `CurrencyProvider` reads the base currency through TanStack Query, so it needs a client above it. */
function renderTab() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <CurrencyProvider>
        <ForecastTab />
      </CurrencyProvider>
    </QueryClientProvider>,
  )
}

/** The digits of a money cell, whatever the currency symbol in front of them. */
function amount(cell: HTMLElement): number {
  return Number(cell.textContent!.replace(/[^\d-]/g, ''))
}

async function oneYearRow() {
  const label = await screen.findByText('1 Year')
  return within(label.closest('tr')!).getAllByRole('cell')
}

describe('ForecastTab', () => {
  it('starts Money In at what was paid in, not at what the book is worth', async () => {
    mockApi()
    renderTab()

    // The table renders before either query answers, so wait for the sentence that can
    // only exist once the baseline has loaded.
    const note = await screen.findByText(/paid in so far/)
    expect(note.textContent).toMatch(/55,000/)
    expect(note.textContent).toMatch(/gain or loss already made/)

    // Column order: horizon, value, gains, money in. One year of 1,000/month on 55,000.
    const [, value, gains, moneyIn] = await oneYearRow()
    expect(amount(moneyIn)).toBe(67_000)
    // The old seed would have read 72,000 + 12,000 here.
    expect(amount(moneyIn)).not.toBe(84_000)
    expect(amount(value)).toBe(amount(moneyIn) + amount(gains))
    expect(amount(gains)).toBeGreaterThan(17_000)

    expect(screen.getByRole('columnheader', { name: /Money In/ })).toBeTruthy()
    expect(screen.queryByText(/Total Contributions/)).toBeNull()
  })

  it('refuses a baseline that failed to load rather than drawing it as zero', async () => {
    mockApi({ contributions: new Error('backend down') })
    renderTab()

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/isn't available/)

    const [, value, gains, moneyIn] = await oneYearRow()
    expect(gains.textContent).toBe('—')
    expect(moneyIn.textContent).toBe('—')
    // Portfolio Value never needed the baseline.
    expect(amount(value)).toBeGreaterThan(72_000)
    expect(screen.queryByText(/paid in so far/)).toBeNull()
  })

  it('shows Total Value on the Current button when cash is tracked, whichever start is selected', async () => {
    mockApi()
    renderTab()

    // Waits for the summary to load: the button reads "Current (…0)" until it has.
    await screen.findByRole('button', { name: /Current \(.*72,000\)/ })

    // Selecting "0" used to relabel this button "Current (…0)".
    const zero = screen.getAllByRole('button').find((b) => /^\D*0$/.test(b.textContent ?? ''))!
    fireEvent.click(zero)
    expect(screen.getByRole('button', { name: /Current/ }).textContent).toMatch(/72,000/)
    expect(await screen.findByText(/Starting from zero/)).toBeTruthy()

    const [, value, gains, moneyIn] = await oneYearRow()
    expect(amount(moneyIn)).toBe(12_000)
    expect(amount(value)).toBe(amount(moneyIn) + amount(gains))
  })

  it('falls back to holdings for the seed when no ledger can derive a cash balance', async () => {
    mockApi({ summary: { ...summary, total_cash_eur: 0, total_value_eur: 60_000, cash_source: 'unknown' } })
    renderTab()

    expect(await screen.findByRole('button', { name: /Current \(.*60,000\)/ })).toBeTruthy()
  })
})
