// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { CompanyTab } from './CompanyTab'
import { CurrencyProvider } from '@/lib/CurrencyProvider'
import { api } from '@/lib/api'
import { COMPANY_PICK_KEY } from '@/lib/companyFinancials'
import {
  FIXTURE_COMPANIES, emptySeriesSet, fixtureFinancials, fixtureSeries,
} from '@/lib/companyFinancials.fixture'

// Recharts draws nothing in jsdom; these tests cover the wiring around the charts.
vi.mock('recharts', async () => {
  const actual = await vi.importActual<typeof import('recharts')>('recharts')
  return { ...actual, ResponsiveContainer: () => <div data-testid="chart" /> }
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  localStorage.clear()
})

beforeEach(() => {
  vi.spyOn(api, 'getLogos').mockResolvedValue({ logos: {}, isins: {} })
  vi.spyOn(api, 'getCompanyFinancialsCompanies').mockResolvedValue(FIXTURE_COMPANIES)
})

function renderTab() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <CurrencyProvider>
        <CompanyTab />
      </CurrencyProvider>
    </QueryClientProvider>,
  )
}

function card(id: string): HTMLElement {
  const el = document.querySelector<HTMLElement>(`[data-chart="${id}"]`)
  if (!el) throw new Error(`no ${id} card`)
  return el
}

describe('CompanyTab', () => {
  it('lists the companies by symbol and name and opens the first at TTM over 10Y', async () => {
    const get = vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials())
    renderTab()
    const select = await screen.findByRole('combobox', { name: 'Company' })
    const options = within(select).getAllByRole('option').map((o) => o.textContent)
    expect(options[0]).toMatch(/^NVDA · Nvidia/i)
    expect(options[1]).toMatch(/^AMZN · Amazon/i)
    expect((select as HTMLSelectElement).value).toBe('11')
    await waitFor(() => expect(get).toHaveBeenCalledWith(11, 'ttm', '10y'))
    expect(await screen.findAllByTestId('chart')).toHaveLength(6)
  })

  it('remembers the pick on this browser and opens it next time', async () => {
    const get = vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials())
    renderTab()
    const select = await screen.findByRole('combobox', { name: 'Company' })
    fireEvent.change(select, { target: { value: '22' } })
    await waitFor(() => expect(get).toHaveBeenCalledWith(22, 'ttm', '10y'))
    expect(localStorage.getItem(COMPANY_PICK_KEY)).toBe('22')

    cleanup()
    get.mockClear()
    renderTab()
    await waitFor(() => expect(get).toHaveBeenCalledWith(22, 'ttm', '10y'))
    expect(get).not.toHaveBeenCalledWith(11, expect.anything(), expect.anything())
  })

  it('ignores a remembered company that is no longer listed', async () => {
    localStorage.setItem(COMPANY_PICK_KEY, '999')
    const get = vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials())
    renderTab()
    await waitFor(() => expect(get).toHaveBeenCalledWith(11, 'ttm', '10y'))
    expect(get).not.toHaveBeenCalledWith(999, expect.anything(), expect.anything())
  })

  it('sends the period and range toggles to the server as query parameters', async () => {
    const get = vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials())
    renderTab()
    await waitFor(() => expect(get).toHaveBeenCalledWith(11, 'ttm', '10y'))

    fireEvent.click(within(screen.getByRole('group', { name: 'Period' })).getByRole('button', { name: 'Quarterly' }))
    await waitFor(() => expect(get).toHaveBeenCalledWith(11, 'quarterly', '10y'))

    fireEvent.click(within(screen.getByRole('group', { name: 'Range' })).getByRole('button', { name: 'Max' }))
    await waitFor(() => expect(get).toHaveBeenCalledWith(11, 'quarterly', 'max'))

    fireEvent.click(within(screen.getByRole('group', { name: 'Period' })).getByRole('button', { name: 'Annual' }))
    await waitFor(() => expect(get).toHaveBeenCalledWith(11, 'annual', 'max'))
    const annual = within(screen.getByRole('group', { name: 'Period' })).getByRole('button', { name: 'Annual' })
    expect(annual.getAttribute('aria-pressed')).toBe('true')
  })

  it("prints the server's CAGR and YoY, and a dash where either is null", async () => {
    vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials())
    renderTab()
    await screen.findAllByTestId('chart')
    expect(card('revenue').textContent).toContain('CAGR 10Y+69.3%')
    expect(card('revenue').textContent).toContain('YoY+114.2%')
    // Net income's CAGR is null (a loss year in the range): a dash, never 0%.
    expect(card('net-income').textContent).toContain('CAGR 10Y—')
    expect(card('net-income').textContent).toContain('YoY+144.9%')
    expect(card('fcf').textContent).toContain('YoY—')
  })

  it('shows the latest margins instead of a CAGR, a dash for a missing one', async () => {
    vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials({
      series: { fcf_margin: fixtureSeries([0.3, 0.14, 0.2, null]) },
    }))
    renderTab()
    await screen.findAllByTestId('chart')
    const margins = card('margins')
    expect(margins.textContent).toContain('Operating62.4%')
    expect(margins.textContent).toContain('Net55.8%')
    expect(margins.textContent).toContain('FCF—')
    expect(margins.textContent).not.toContain('CAGR')
  })

  it('tones a falling share count as the gain it is', async () => {
    vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials())
    renderTab()
    await screen.findAllByTestId('chart')
    const cagr = within(card('shares')).getByText('-0.7%')
    expect(cagr.className).toMatch(/text-green-600/)
  })

  it('hides the dividends card for a company that pays none', async () => {
    vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials({
      series: { dividends_per_share: fixtureSeries([null, null, null, null]) },
    }))
    renderTab()
    expect(await screen.findAllByTestId('chart')).toHaveLength(5)
    expect(document.querySelector('[data-chart="dps"]')).toBeNull()
    expect(screen.queryByText('Dividends per share')).toBeNull()
  })

  it('switches every figure to per share without asking the server again', async () => {
    const get = vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials())
    renderTab()
    await screen.findAllByTestId('chart')
    expect(within(card('revenue')).getByTestId('headline').textContent).toBe('$130.5B')
    const calls = get.mock.calls.length

    fireEvent.click(within(screen.getByRole('group', { name: 'Basis' })).getByRole('button', { name: 'Per share' }))
    expect(card('revenue').textContent).toContain('Revenue per share')
    expect(within(card('revenue')).getByTestId('headline').textContent).toBe('$5.26')
    expect(card('net-income').textContent).toContain('EPS (diluted)')
    expect(within(card('net-income')).getByTestId('headline').textContent).toBe('$2.94')
    expect(card('fcf').textContent).toContain('FCF per share')
    expect(card('shares').textContent).toContain('Diluted shares')
    expect(get.mock.calls.length).toBe(calls)
  })

  it('hides a series from its legend but never the last one', async () => {
    vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials())
    renderTab()
    await screen.findAllByTestId('chart')
    const legend = within(card('margins')).getByRole('group', { name: 'Margins series' })
    const [operating, net, fcf] = within(legend).getAllByRole('button')
    fireEvent.click(operating)
    fireEvent.click(net)
    expect(operating.getAttribute('aria-pressed')).toBe('false')
    expect(net.getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(fcf)
    expect(fcf.getAttribute('aria-pressed')).toBe('true')
    // The headline follows the first series still drawn.
    expect(within(card('margins')).getByTestId('headline').textContent).toBe('46.6%')
  })

  it('expands one card at a time', async () => {
    vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials())
    renderTab()
    await screen.findAllByTestId('chart')
    fireEvent.click(screen.getByRole('button', { name: 'Expand Revenue' }))
    expect(card('revenue').className).toMatch(/col-span-2/)
    fireEvent.click(screen.getByRole('button', { name: 'Expand Net income' }))
    expect(card('revenue').className).not.toMatch(/col-span-2/)
    expect(card('net-income').className).toMatch(/col-span-2/)
    fireEvent.click(screen.getByRole('button', { name: 'Shrink Net income' }))
    expect(card('net-income').className).not.toMatch(/col-span-2/)
  })

  it('shows a warning and no charts for a company with no data', async () => {
    vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials({ series: emptySeriesSet() }))
    renderTab()
    expect((await screen.findByRole('alert')).textContent).toContain('No financial data for NVDA.')
    expect(screen.queryAllByTestId('chart')).toHaveLength(0)
  })

  it("renders the response's warnings above the charts", async () => {
    vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials({
      warnings: ['FY2024 cash flow not filed'],
    }))
    renderTab()
    expect((await screen.findByRole('alert')).textContent).toContain('FY2024 cash flow not filed')
    expect(await screen.findAllByTestId('chart')).toHaveLength(6)
  })

  it('has no warning when the response carries none', async () => {
    vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials())
    renderTab()
    await screen.findAllByTestId('chart')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('says the companies could not load rather than showing an empty picker', async () => {
    vi.spyOn(api, 'getCompanyFinancialsCompanies').mockRejectedValue(new Error('HTTP 500'))
    const get = vi.spyOn(api, 'getCompanyFinancials')
    renderTab()
    expect(await screen.findByText(/Couldn't load the companies/)).toBeTruthy()
    expect(get).not.toHaveBeenCalled()
  })

  it("says one company's financials could not load", async () => {
    vi.spyOn(api, 'getCompanyFinancials').mockRejectedValue(new Error('Not Found'))
    renderTab()
    expect(await screen.findByText(/Couldn't load NVDA's financials/)).toBeTruthy()
    expect(screen.queryAllByTestId('chart')).toHaveLength(0)
  })
})
