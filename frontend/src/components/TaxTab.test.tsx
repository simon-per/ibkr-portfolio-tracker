// @vitest-environment jsdom
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { render, screen, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { TaxTab, estimatedDividendRows, withholdingTotalLabel } from './TaxTab'
import { CurrencyProvider } from '@/lib/CurrencyContext'
import { api } from '@/lib/api'
import type { TaxReport, TaxDividendRow } from '@/lib/api'

/**
 * An estimated dividend row has a guessed gross and no withholding on record. Its 0 is
 * "not known", so the cell is absent and the total says what it left out — a 0.00 there
 * reads as "nothing was withheld", which on a DA-1 aid is a claim against the reclaim.
 */

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

beforeEach(() => {
  if (!('ResizeObserver' in globalThis)) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
  }
  vi.spyOn(api, 'getSettings').mockResolvedValue({
    base_currency: 'CHF',
    supported_currencies: ['EUR', 'CHF', 'USD'],
  } as Awaited<ReturnType<typeof api.getSettings>>)
  vi.spyOn(api, 'getContributions').mockRejectedValue(new Error('not needed'))
})

const money = (n: number) => `€${n.toFixed(2)}`

function row(over: Partial<TaxDividendRow>): TaxDividendRow {
  return {
    symbol: 'NESN',
    description: 'NESTLE',
    isin: 'CH0038863350',
    pay_date: '2026-04-20',
    gross: 100,
    withholding: 35,
    net: 65,
    source: 'ibkr',
    ...over,
  }
}

function report(over: Partial<TaxReport> = {}): TaxReport {
  return {
    year: 2026,
    base_currency: 'CHF',
    dividend_source: 'mixed',
    dividend_ibkr_from: '2026-02-15',
    dividend_income: [
      row({ symbol: 'EST', pay_date: '2026-01-10', withholding: 0, net: 50, gross: 50, source: 'yfinance_estimate' }),
      row({}),
    ],
    dividend_by_country: [{ country: 'CH', positions: 2, gross: 150, withholding: 35, net: 115 }],
    dividend_country_note: 'Country from the ISIN prefix — verify ADRs and funds.',
    dividend_totals: { gross: 150, withholding: 35, net: 115 },
    realized_gains: [
      { symbol: 'LOSS', trade_date: '2026-03-01', quantity: 1, proceeds: 90, cost_basis: 100, gain_loss: -10 },
    ],
    realized_source: 'trades',
    holdings_as_of: '2026-10-14',
    realized_totals: { proceeds: 90, cost_basis: 100, gain_loss: -10 },
    holdings_snapshot: [],
    holdings_snapshot_total: 0,
    holdings_snapshot_note: '',
    pillar3a: null,
    warnings: [],
    ...over,
  }
}

function renderTab() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <CurrencyProvider>
        <TaxTab />
      </CurrencyProvider>
    </QueryClientProvider>,
  )
}

/** The table whose header row names `header`. */
async function tableWithHeader(header: string): Promise<HTMLElement> {
  const th = await screen.findByRole('columnheader', { name: header })
  return th.closest('table') as HTMLElement
}

describe('withholdingTotalLabel', () => {
  it('is absent when every row is an estimate', () => {
    expect(withholdingTotalLabel(money, 0, 3, 3)).toEqual({ amount: '—', excluded: null })
  })

  it('counts the estimates it excludes in a mixed year', () => {
    expect(withholdingTotalLabel(money, 35, 4, 1)).toEqual({ amount: '-€35.00', excluded: 'excl. 1 est.' })
  })

  it('is a plain figure when every row is recorded, and a recorded zero stays zero', () => {
    expect(withholdingTotalLabel(money, 35, 2, 0)).toEqual({ amount: '-€35.00', excluded: null })
    expect(withholdingTotalLabel(money, 0, 2, 0)).toEqual({ amount: '€0.00', excluded: null })
  })

  it('counts estimated rows by their source', () => {
    expect(estimatedDividendRows(report().dividend_income)).toBe(1)
  })
})

describe('TaxTab', () => {
  it('shows an absent withholding on an estimated row and tags the total', async () => {
    vi.spyOn(api, 'getTaxReport').mockResolvedValue(report())
    renderTab()
    const table = await tableWithHeader('Pay date')
    const est = within(table).getByText('EST').closest('tr')!
    const cells = est.querySelectorAll('td')
    expect(cells[cells.length - 1].textContent).toBe('—')
    expect(within(table).getByText('excl. 1 est.')).toBeTruthy()
    // The country column cannot tell its estimates apart, so the boundary year tags it.
    const byCountry = await tableWithHeader('Country')
    expect(within(byCountry).getByText('excl. est.')).toBeTruthy()
  })

  it('leaves the withholding total and every country absent in an estimate-only year', async () => {
    vi.spyOn(api, 'getTaxReport').mockResolvedValue(
      report({
        dividend_source: 'yfinance_estimate',
        dividend_income: [row({ withholding: 0, net: 100, source: 'yfinance_estimate' })],
        dividend_by_country: [{ country: 'CH', positions: 1, gross: 100, withholding: 0, net: 100 }],
        dividend_totals: { gross: 100, withholding: 0, net: 100 },
      }),
    )
    renderTab()
    const table = await tableWithHeader('Pay date')
    const footer = table.querySelector('tfoot')!
    expect(within(footer).getByText('—')).toBeTruthy()
    const byCountry = await tableWithHeader('Country')
    expect(within(byCountry).getByText('—')).toBeTruthy()
  })

  it('puts the sign before the currency, as formatCurrency does', async () => {
    vi.spyOn(api, 'getTaxReport').mockResolvedValue(report())
    renderTab()
    const table = await tableWithHeader('Trade date')
    expect((await within(table).findAllByText(/^-CHF\s10\.00$/)).length).toBeGreaterThan(0)
    expect(within(table).queryByText(/CHF\s-/)).toBeNull()
  })

  it('does not render the country-of-ISIN methodology note', async () => {
    vi.spyOn(api, 'getTaxReport').mockResolvedValue(report())
    renderTab()
    await tableWithHeader('Country')
    expect(screen.queryByText(/ISIN prefix/)).toBeNull()
  })
})
