// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { CompanyScreenerCard } from './CompanyScreenerCard'
import { api, type CompanyFinancialsCompany, type CompanyScreenerRow } from '@/lib/api'
import { CompanyLinksContext, companyLinks } from '@/lib/companyLinks'

const spans = (y3: number | null, y5: number | null, y10: number | null) => ({ y3, y5, y10 })
const NONE = spans(null, null, null)

function company(cik: number, symbol: string, figures: Partial<CompanyScreenerRow>): CompanyScreenerRow {
  return {
    cik, security_id: null, symbol, name: `${symbol} Inc`, isin: null, held: false,
    currency: 'USD', industry: 'Semiconductors', period_end: '2026-09-30', market_cap: 1e12,
    revenue_yoy: null, revenue_cagr_5y: 0.2, eps_yoy: null, eps_cagr_5y: 0.3, gross_margin: null,
    operating_margin: null, fcf_margin: 0.3, roe: 0.4, net_cash: null, pe_ratio: 30,
    pe_median: spans(null, 30, null), pe_vs_median: spans(null, 0, null), pfcf_ratio: null,
    pfcf_median: NONE, pfcf_vs_median: NONE, ps_ratio: null, ps_median: NONE, ps_vs_median: NONE,
    dividend_yield: null, dps_cagr_5y: null, raise_years: null, payout_ratio: null,
    ...figures,
  }
}

const ROWS: CompanyScreenerRow[] = [
  company(1, 'NVDA', {
    pe_ratio: 45, pe_median: spans(41, 50, 64), pe_vs_median: spans(0.1, -0.1, -0.3), held: true,
  }),
  company(2, 'AVGO', { pe_ratio: null, pe_median: spans(28, 30, null), pe_vs_median: NONE }),
  company(3, 'AMD', {
    pe_ratio: 90, pe_median: spans(150, 60, 75), pe_vs_median: spans(-0.4, 0.5, 0.2),
  }),
  company(4, 'INTC', {
    pe_ratio: 12, pe_median: spans(10, 15, null), pe_vs_median: spans(0.2, -0.2, null), held: true,
  }),
]

// The tab's company list, which every screener row is on: the links resolve through it.
const LINKS = companyLinks(
  ROWS.map((r) => ({ ...r, security_ids: [] }) as unknown as CompanyFinancialsCompany),
)

function renderCard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <CompanyLinksContext.Provider value={LINKS}>
        <CompanyScreenerCard />
      </CompanyLinksContext.Provider>
    </QueryClientProvider>,
  )
}

const symbols = (table: HTMLElement) =>
  within(table).getAllByRole('link').map((a) => a.querySelector('.font-medium')?.textContent)

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('CompanyScreenerCard', () => {
  it('asks the server only once it is opened', async () => {
    const get = vi.spyOn(api, 'getCompanyScreener').mockResolvedValue(ROWS)
    renderCard()
    expect(get).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: /Screener/ }))
    await screen.findByTestId('company-screener')
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1))
    expect(await screen.findByText('4 companies')).toBeTruthy()
  })

  it('opens on the cheapest against their own 5-year P/E, a company with no P/E last', async () => {
    vi.spyOn(api, 'getCompanyScreener').mockResolvedValue(ROWS)
    renderCard()
    fireEvent.click(screen.getByRole('button', { name: /Screener/ }))
    const table = await screen.findByRole('table')
    expect(symbols(table)).toEqual(['INTC', 'NVDA', 'AMD', 'AVGO'])
    expect(within(table).getAllByText('-20.0%').length).toBeGreaterThan(0)
    expect(within(table).getAllByRole('link')[0].getAttribute('href')).toBe('#company/4')

    // Highest P/E first once that column is chosen; the blank stays last.
    fireEvent.click(within(table).getByRole('button', { name: /^P\/E$/ }))
    expect(symbols(table)).toEqual(['AMD', 'NVDA', 'INTC', 'AVGO'])
  })

  it('reads today against another span of its own history, sorted the same way', async () => {
    vi.spyOn(api, 'getCompanyScreener').mockResolvedValue(ROWS)
    renderCard()
    fireEvent.click(screen.getByRole('button', { name: /Screener/ }))
    const table = await screen.findByRole('table')

    fireEvent.click(screen.getByRole('button', { name: '10Y' }))
    expect(within(table).getByRole('button', { name: /^vs 10Y$/ })).toBeTruthy()
    expect(within(table).queryByRole('button', { name: /^vs 5Y$/ })).toBeNull()
    // Cheapest against ten years first; INTC has no ten-year median, so it joins AVGO last.
    expect(symbols(table)).toEqual(['NVDA', 'AMD', 'AVGO', 'INTC'])
    expect(within(table).getAllByText('64.0').length).toBeGreaterThan(0)  // NVDA's own median

    fireEvent.click(screen.getByRole('button', { name: '3Y' }))
    expect(symbols(table)).toEqual(['AMD', 'NVDA', 'INTC', 'AVGO'])
  })

  it('narrows to the held companies', async () => {
    vi.spyOn(api, 'getCompanyScreener').mockResolvedValue(ROWS)
    renderCard()
    fireEvent.click(screen.getByRole('button', { name: /Screener/ }))
    await screen.findByRole('table')
    fireEvent.click(screen.getByRole('button', { name: 'Held' }))
    expect(symbols(screen.getByRole('table'))).toEqual(['INTC', 'NVDA'])
  })
})
