// @vitest-environment jsdom
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { render, screen, cleanup, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { TaxTab } from './TaxTab'
import { includedEstimatesTag, withholdingTotalLabel } from '@/lib/taxTotals'
import { CurrencyProvider } from '@/lib/CurrencyProvider'
import { api } from '@/lib/api'
import type { TaxReport, TaxDividendRow, TaxCountryRow } from '@/lib/api'

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

function country(over: Partial<TaxCountryRow>): TaxCountryRow {
  return {
    country: 'CH',
    positions: 2,
    gross: 150,
    withholding: 35,
    rate_pct: 35,
    residual_pct: null,
    creditable: null,
    excess: null,
    net: 115,
    payments: 2,
    estimated_payments: 1,
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
    dividend_by_country: [country({})],
    dividend_country_note: "Country from IBKR's withholding line, else the ISIN prefix.",
    dividend_totals: { gross: 150, withholding: 35, net: 115, estimated_rows: 1 },
    realized_gains: [
      {
        symbol: 'LOSS', security_id: null, isin: null, trade_date: '2026-03-01', quantity: 1,
        proceeds: 90, cost_basis: 100, gain_loss: -10,
      },
    ],
    realized_source: 'trades',
    holdings_as_of: '2026-10-14',
    realized_totals: { proceeds: 90, cost_basis: 100, gain_loss: -10 },
    holdings_snapshot: [],
    holdings_snapshot_total: 0,
    holdings_snapshot_note: '',
    cash_balance: null,
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
          country({}),
          country({ country: 'US', positions: 1, gross: 80, withholding: 12, net: 68, payments: 1, estimated_payments: 0 }),
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
          country({ positions: 1, gross: 100, withholding: null, rate_pct: null, net: 100, payments: 1 }),
        ],
        dividend_totals: { gross: 100, withholding: null, net: 100, estimated_rows: 1 },
      }),
    )
    renderTab()
    const table = await tableWithHeader('Pay date')
    const footer = table.querySelector('tfoot')!
    expect(within(footer).getByText('—')).toBeTruthy()
    const byCountry = await tableWithHeader('Country')
    // Withholding, rate, creditable and excess: nothing recorded, so nothing printed.
    expect(within(byCountry).getAllByText('—')).toHaveLength(4)
    expect(byCountry.textContent).not.toMatch(/CHF\s0\.00|0\.00%/)
  })

  it('splits a country withheld above its treaty rate into creditable and excess', async () => {
    vi.spyOn(api, 'getTaxReport').mockResolvedValue(
      report({
        dividend_by_country: [
          country({
            country: 'KR', positions: 1, gross: 300, withholding: 66, rate_pct: 22,
            residual_pct: 15, creditable: 45, excess: 21, net: 234, payments: 2,
            estimated_payments: 0,
          }),
          country({
            country: 'TW', positions: 1, gross: 100, withholding: 21, rate_pct: 21,
            net: 79, payments: 1, estimated_payments: 0,
          }),
        ],
      }),
    )
    renderTab()
    const byCountry = await tableWithHeader('Creditable')
    const headers = Array.from(byCountry.querySelectorAll('thead th')).map((th) => th.textContent)
    expect(headers).toEqual(expect.arrayContaining(['Rate', 'Creditable', 'Excess']))
    const cellUnder = (rowText: RegExp, header: string): string => {
      const tr = Array.from(byCountry.querySelectorAll('tbody tr')).find((r) => rowText.test(r.textContent ?? ''))!
      return tr.querySelectorAll('td')[headers.indexOf(header)].textContent ?? ''
    }
    expect(cellUnder(/^KR/, 'Rate')).toBe('22.00%')
    expect(cellUnder(/^KR/, 'Creditable')).toMatch(/^CHF\s45\.00$/)
    expect(cellUnder(/^KR/, 'Excess')).toMatch(/^CHF\s21\.00$/)
    // No treaty rate on record: the rate is a fact, the split is not guessed.
    expect(cellUnder(/^TW/, 'Rate')).toBe('21.00%')
    expect(cellUnder(/^TW/, 'Creditable')).toBe('—')
    expect(cellUnder(/^TW/, 'Excess')).toBe('—')
  })

  it('shows the year-end cash beside the holdings, and nothing when it is unknown', async () => {
    const holdings = [
      { symbol: 'NESN', security_id: 7, isin: 'CH0038863350', quantity: 3, market_value: 240, cost_basis: 270 },
    ]
    vi.spyOn(api, 'getTaxReport').mockResolvedValue(
      report({ holdings_snapshot: holdings, holdings_snapshot_total: 240, cash_balance: -12.5 }),
    )
    renderTab()
    const table = await tableWithHeader('Market value')
    const card = table.closest('.space-y-6 > *') as HTMLElement
    const line = within(card).getByText('Cash').parentElement!
    expect(line.textContent).toMatch(/-CHF\s12\.50/)
    cleanup()

    vi.spyOn(api, 'getTaxReport').mockResolvedValue(
      report({ holdings_snapshot: holdings, holdings_snapshot_total: 240, cash_balance: null }),
    )
    renderTab()
    await tableWithHeader('Market value')
    expect(screen.queryByText('Cash')).toBeNull()
  })

  it('dashes a pillar 3a value that could not be built', async () => {
    vi.spyOn(api, 'getTaxReport').mockResolvedValue(
      report({
        pillar3a: { tracked: true, accounts: ['pillar3a'], contributions: 1758, holdings_value: null, note: '' },
      }),
    )
    renderTab()
    const label = await screen.findByText('Assets held')
    expect(label.parentElement!.textContent).toContain('—')
    expect(label.parentElement!.textContent).not.toMatch(/CHF\s0\.00/)
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
        holdings_snapshot: [
          { symbol: 'NESN', security_id: 7, isin: 'CH0038863350', quantity: 3, market_value: 240, cost_basis: 270 },
        ],
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

describe('the year summary', () => {
  /** A KPI tile: the summary panel's direct child holding `label`. */
  function tile(label: string): HTMLElement {
    let el: HTMLElement = screen.getByText(label)
    while (el.parentElement && !el.parentElement.className.includes('gap-px')) el = el.parentElement
    return el
  }

  it('repeats each totals row with the marks that row carries', async () => {
    vi.spyOn(api, 'getTaxReport').mockResolvedValue(report())
    renderTab()
    await tableWithHeader('Pay date')
    expect(tile('Dividends, net').textContent).toMatch(/CHF\s115\.00/)
    expect(tile('Dividends, net').textContent).toContain('incl. 1 est.')
    expect(tile('Withheld').textContent).toMatch(/-CHF\s35\.00/)
    expect(tile('Withheld').textContent).toContain('excl. 1 est.')
    expect(tile('Realized gains').textContent).toMatch(/-CHF\s10\.00/)
    expect(tile('Realized gains').textContent).toContain('1 sale')
    expect(tile('Steuerwert').textContent).toContain('as at 2026-10-14')
  })

  it('dashes what is unknown rather than printing a zero', async () => {
    vi.spyOn(api, 'getTaxReport').mockResolvedValue(
      report({
        dividend_source: 'yfinance_estimate',
        dividend_income: [row({ withholding: null, net: 100, source: 'yfinance_estimate' })],
        dividend_totals: { gross: 100, withholding: null, net: 100, estimated_rows: 1 },
        holdings_snapshot_error: true,
        holdings_snapshot_total: null,
      }),
    )
    renderTab()
    await tableWithHeader('Pay date')
    expect(tile('Withheld').textContent).toContain('—')
    expect(tile('Withheld').textContent).not.toContain('0.00')
    expect(tile('Steuerwert').textContent).toContain('—')
    expect(tile('Steuerwert').textContent).not.toContain('0.00')
    // Every row an estimate: the figure is one, whole.
    expect(tile('Dividends, net').textContent).toContain('est.')
  })

  it('says a year without sales has none, rather than signing a zero', async () => {
    vi.spyOn(api, 'getTaxReport').mockResolvedValue(
      report({ realized_gains: [], realized_totals: { proceeds: 0, cost_basis: 0, gain_loss: 0 } }),
    )
    renderTab()
    await tableWithHeader('Pay date')
    expect(tile('Realized gains').textContent).toContain('No sales')
    expect(tile('Realized gains').textContent).not.toContain('+')
  })
})

describe('logos in the tax tables', () => {
  it('draws the dividend row by its ISIN, and the rows without one as a monogram', async () => {
    vi.spyOn(api, 'getLogos').mockResolvedValue({ logos: {}, isins: { CH0038863350: 'v1' } })
    vi.spyOn(api, 'getTaxReport').mockResolvedValue(report())
    renderTab()
    const income = await tableWithHeader('Pay date')
    await waitFor(() => expect(income.querySelectorAll('img').length).toBeGreaterThan(0))
    expect(income.querySelector('img')!.getAttribute('src')).toContain('CH0038863350')
    // A sale linked to no security has no ISIN and no id: a monogram, never a broken image.
    const realized = await tableWithHeader('Trade date')
    expect(realized.querySelectorAll('img')).toHaveLength(0)
    expect(within(realized).getByText('LO')).toBeTruthy()
  })

  it('draws the realized and holdings rows by their security id', async () => {
    vi.spyOn(api, 'getLogos').mockResolvedValue({ logos: { '7': 'v2' }, isins: {} })
    vi.spyOn(api, 'getTaxReport').mockResolvedValue(
      report({
        realized_gains: [
          {
            symbol: 'NESN', security_id: 7, isin: 'CH0038863350', trade_date: '2026-03-01',
            quantity: 1, proceeds: 90, cost_basis: 100, gain_loss: -10,
          },
        ],
        holdings_snapshot: [
          { symbol: 'NESN', security_id: 7, isin: 'CH0038863350', quantity: 3, market_value: 240, cost_basis: 270 },
        ],
        holdings_snapshot_total: 240,
      }),
    )
    renderTab()
    for (const header of ['Trade date', 'Market value']) {
      const table = await tableWithHeader(header)
      await waitFor(() => expect(table.querySelectorAll('img').length).toBe(1))
      expect(table.querySelector('img')!.getAttribute('src')).toContain('/api/logos/7?v=v2')
    }
  })
})
