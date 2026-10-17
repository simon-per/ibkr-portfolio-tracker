// @vitest-environment jsdom
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { render, screen, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { TaxTab } from './TaxTab'
import { includedEstimatesTag, withholdingTotalLabel } from '@/lib/taxTotals'
import { CurrencyProvider } from '@/lib/CurrencyProvider'
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
      row({ symbol: 'EST', pay_date: '2026-01-10', withholding: null, net: 50, gross: 50, source: 'yfinance_estimate' }),
      row({}),
    ],
    dividend_by_country: [
      { country: 'CH', positions: 2, gross: 150, withholding: 35, net: 115, payments: 2, estimated_payments: 1 },
    ],
    dividend_country_note: 'Country from the ISIN prefix — verify ADRs and funds.',
    dividend_totals: { gross: 150, withholding: 35, net: 115, estimated_rows: 1 },
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
  it('is absent when the server has no recorded withholding (every row an estimate)', () => {
    expect(withholdingTotalLabel(money, null, 3)).toEqual({ amount: '—', excluded: null })
  })

  it('counts the estimates it excludes in a mixed year', () => {
    expect(withholdingTotalLabel(money, 35, 1)).toEqual({ amount: '-€35.00', excluded: 'excl. 1 est.' })
  })

  it('is a plain figure when every row is recorded, and a recorded zero stays zero', () => {
    expect(withholdingTotalLabel(money, 35, 0)).toEqual({ amount: '-€35.00', excluded: null })
    expect(withholdingTotalLabel(money, 0, 0)).toEqual({ amount: '€0.00', excluded: null })
  })
})

describe('includedEstimatesTag', () => {
  it('tags a total that mixes estimates with recorded rows', () => {
    expect(includedEstimatesTag(1, 2)).toBe('incl. 1 est.')
  })

  it('is absent when nothing is an estimate, or everything is (the card is badged then)', () => {
    expect(includedEstimatesTag(0, 2)).toBeNull()
    expect(includedEstimatesTag(3, 3)).toBeNull()
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
    // Gross and net include the estimate, and say so.
    expect(within(table.querySelector('tfoot')!).getAllByText('incl. 1 est.')).toHaveLength(2)
    const byCountry = await tableWithHeader('Country')
    expect(within(byCountry).getByText('excl. est.')).toBeTruthy()
  })

  it('tags only the countries whose payments include an estimate', async () => {
    vi.spyOn(api, 'getTaxReport').mockResolvedValue(
      report({
        dividend_by_country: [
          { country: 'CH', positions: 2, gross: 150, withholding: 35, net: 115, payments: 2, estimated_payments: 1 },
          { country: 'US', positions: 1, gross: 80, withholding: 12, net: 68, payments: 1, estimated_payments: 0 },
        ],
      }),
    )
    renderTab()
    const byCountry = await tableWithHeader('Country')
    const tagged = within(byCountry).getAllByText('excl. est.').map((el) => el.closest('tr')!.textContent)
    expect(tagged).toHaveLength(1)
    expect(tagged[0]).toMatch(/^CH/)
  })

  it('leaves the withholding total and every country absent in an estimate-only year', async () => {
    vi.spyOn(api, 'getTaxReport').mockResolvedValue(
      report({
        dividend_source: 'yfinance_estimate',
        dividend_income: [row({ withholding: null, net: 100, source: 'yfinance_estimate' })],
        dividend_by_country: [
          { country: 'CH', positions: 1, gross: 100, withholding: null, net: 100, payments: 1, estimated_payments: 1 },
        ],
        dividend_totals: { gross: 100, withholding: null, net: 100, estimated_rows: 1 },
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

  it('puts every total under the header of the column it sums', async () => {
    // Each total sat under a neighbour's header from 2026-10-12 to 2026-10-17: the
    // footers were laid out in array order and listed in a different order from the
    // columns. Read the header text at each footer cell's starting column.
    vi.spyOn(api, 'getTaxReport').mockResolvedValue(
      report({
        dividend_source: 'ibkr',
        dividend_income: [row({})],
        dividend_totals: { gross: 100, withholding: 35, net: 65, estimated_rows: 0 },
        holdings_snapshot: [{ symbol: 'NESN', quantity: 3, market_value: 240, cost_basis: 270 }],
        holdings_snapshot_total: 240,
      }),
    )
    renderTab()

    const headerAt = (table: HTMLElement, text: string): string => {
      const footerCells = Array.from(table.querySelectorAll('tfoot td'))
      let column = 0
      for (const td of footerCells) {
        if ((td.textContent ?? '').replace(/\s/g, ' ') === text) {
          return table.querySelectorAll('thead th')[column].textContent ?? ''
        }
        column += Number(td.getAttribute('colspan') ?? 1)
      }
      throw new Error(`no footer cell reads ${text}`)
    }

    const income = await tableWithHeader('Pay date')
    expect(headerAt(income, 'CHF 65.00')).toMatch(/^Net/)
    expect(headerAt(income, 'CHF 100.00')).toMatch(/^Gross/)
    expect(headerAt(income, '-CHF 35.00')).toMatch(/^Withholding/)

    const realized = await tableWithHeader('Trade date')
    expect(headerAt(realized, '-CHF 10.00')).toMatch(/^Gain/)
    expect(headerAt(realized, 'CHF 90.00')).toMatch(/^Proceeds/)
    expect(headerAt(realized, 'CHF 100.00')).toMatch(/^Cost basis/)

    const holdings = await tableWithHeader('Market value')
    expect(headerAt(holdings, 'CHF 240.00')).toMatch(/^Market value/)
  })

  it('does not render the country-of-ISIN methodology note', async () => {
    vi.spyOn(api, 'getTaxReport').mockResolvedValue(report())
    renderTab()
    await tableWithHeader('Country')
    expect(screen.queryByText(/ISIN prefix/)).toBeNull()
  })
})
