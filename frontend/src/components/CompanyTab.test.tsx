// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { CompanyTab } from './CompanyTab'
import { CurrencyProvider } from '@/lib/CurrencyProvider'
import { api } from '@/lib/api'
import { COMPANY_PICK_KEY } from '@/lib/companyFinancials'
import {
  AMZN_CIK, FIXTURE_COMPANIES, FIXTURE_KEY_STATS, NVDA_CIK, emptySeriesSet, fixtureFinancials, fixtureSeries,
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
  it('lists held companies before the Nasdaq-100 and opens the first held at TTM over 10Y', async () => {
    const get = vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials())
    renderTab()
    const select = await screen.findByRole('combobox', { name: 'Company' })
    const groups = within(select).getAllByRole('group').map((g) => g.getAttribute('label'))
    expect(groups).toEqual(['Held', 'Nasdaq-100'])
    const options = within(select).getAllByRole('option').map((o) => o.textContent)
    expect(options[0]).toMatch(/^NVDA · Nvidia/i)
    expect(options[1]).toMatch(/^AMZN · Amazon/i)
    expect(options[2]).toMatch(/^AAPL · Apple/i)
    expect((select as HTMLSelectElement).value).toBe(String(NVDA_CIK))
    await waitFor(() => expect(get).toHaveBeenCalledWith(NVDA_CIK, 'ttm', '10y'))
    expect(await screen.findAllByTestId('chart')).toHaveLength(11)
  })

  it('remembers the pick on this browser and opens it next time', async () => {
    const get = vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials())
    renderTab()
    const select = await screen.findByRole('combobox', { name: 'Company' })
    fireEvent.change(select, { target: { value: String(AMZN_CIK) } })
    await waitFor(() => expect(get).toHaveBeenCalledWith(AMZN_CIK, 'ttm', '10y'))
    expect(localStorage.getItem(COMPANY_PICK_KEY)).toBe(String(AMZN_CIK))

    cleanup()
    get.mockClear()
    renderTab()
    await waitFor(() => expect(get).toHaveBeenCalledWith(AMZN_CIK, 'ttm', '10y'))
    expect(get).not.toHaveBeenCalledWith(NVDA_CIK, expect.anything(), expect.anything())
  })

  it('ignores a remembered company that is no longer listed', async () => {
    localStorage.setItem(COMPANY_PICK_KEY, '999')
    const get = vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials())
    renderTab()
    await waitFor(() => expect(get).toHaveBeenCalledWith(NVDA_CIK, 'ttm', '10y'))
    expect(get).not.toHaveBeenCalledWith(999, expect.anything(), expect.anything())
  })

  it('sends the period and range toggles to the server as query parameters', async () => {
    const get = vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials())
    renderTab()
    await waitFor(() => expect(get).toHaveBeenCalledWith(NVDA_CIK, 'ttm', '10y'))

    fireEvent.click(within(screen.getByRole('group', { name: 'Period' })).getByRole('button', { name: 'Quarterly' }))
    await waitFor(() => expect(get).toHaveBeenCalledWith(NVDA_CIK, 'quarterly', '10y'))

    fireEvent.click(within(screen.getByRole('group', { name: 'Range' })).getByRole('button', { name: 'Max' }))
    await waitFor(() => expect(get).toHaveBeenCalledWith(NVDA_CIK, 'quarterly', 'max'))

    fireEvent.click(within(screen.getByRole('group', { name: 'Period' })).getByRole('button', { name: 'Annual' }))
    await waitFor(() => expect(get).toHaveBeenCalledWith(NVDA_CIK, 'annual', 'max'))
    const annual = within(screen.getByRole('group', { name: 'Period' })).getByRole('button', { name: 'Annual' })
    expect(annual.getAttribute('aria-pressed')).toBe('true')
  })

  it("prints the server's CAGRs and YoY, and leaves out a CAGR it has none for", async () => {
    vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials({
      series: { revenue: { ...fixtureSeries([1, 2, 3, 4], 0.693, 1.142), cagrs: { y3: 0.81, y5: 0.642, y10: 0.693 } } },
    }))
    renderTab()
    await screen.findAllByTestId('chart')
    expect(within(card('revenue')).getByTestId('cagrs').textContent).toBe('CAGR3Y+81.0%5Y+64.2%10Y+69.3%')
    expect(card('revenue').textContent).toContain('YoY+114.2%')
    // Net income has no CAGR (a loss year): none printed, never 0%.
    expect(within(card('net-income')).queryByTestId('cagrs')).toBeNull()
    expect(card('net-income').textContent).toContain('YoY+144.9%')
    expect(card('fcf').textContent).toContain('YoY—')
  })

  it('shows the key TTM figures above the charts and leaves out those not filed', async () => {
    vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials())
    renderTab()
    const stats = await screen.findByTestId('key-stats')
    expect(stats.textContent).toContain('TTM · Q4 FY2025')
    expect(stats.textContent).toContain('Revenue$130.5B')
    expect(stats.textContent).toContain('YoY+114.2%')
    expect(stats.textContent).toContain('EPS$2.94')
    expect(stats.textContent).toContain('P/E48.3')
    expect(stats.textContent).toContain('P/S26.9')
    expect(stats.textContent).toContain('Market cap$3.5T')
    expect(stats.textContent).toContain('Enterprise value$3.5T')
    expect(stats.textContent).toContain('Net cash$34.8B')
    expect(stats.textContent).toContain('EV/EBIT42.6')
    expect(stats.textContent).toContain('P/FCF57.6')
    expect(stats.textContent).toContain('Return on equity119.2%')
    expect(stats.textContent).toContain('Dividend yield0.02%')
    expect(stats.textContent).toContain('Payout1.2%')
    expect(stats.textContent).toContain('Dividends paid$834M')
    expect(stats.textContent).not.toContain('Gross margin')
    expect(stats.textContent).not.toContain('Buybacks')
    // A falling share count is the gain it is.
    expect(within(stats).getByText('-0.5%').className).toMatch(/text-green-600/)
  })

  it('shows the latest margins instead of a CAGR, a dash for a missing one', async () => {
    vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials({
      series: { fcf_margin: fixtureSeries([0.3, 0.14, 0.2, null]) },
    }))
    renderTab()
    await screen.findAllByTestId('chart')
    const margins = card('margins')
    expect(margins.textContent).toContain('Gross75.0%')
    expect(margins.textContent).toContain('Operating62.4%')
    expect(margins.textContent).toContain('Net55.8%')
    expect(margins.textContent).toContain('FCF—')
    expect(margins.textContent).not.toContain('CAGR')
  })

  it('draws the multiples as the latest values, without a CAGR', async () => {
    vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials())
    renderTab()
    await screen.findAllByTestId('chart')
    expect(within(card('valuation')).getByTestId('headline').textContent).toBe('48.3')
    expect(card('valuation').textContent).toContain('P/FCF57.6')
    expect(card('valuation').textContent).toContain('P/S26.9')
    expect(card('valuation').textContent).not.toContain('CAGR')
    expect(within(card('roe')).getByTestId('headline').textContent).toBe('119.2%')
  })

  it('hides the valuation card for a company without stored prices', async () => {
    const none = fixtureSeries([null, null, null, null])
    vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials({
      series: { pe_ratio: none, ps_ratio: none, pfcf_ratio: none },
    }))
    renderTab()
    expect(await screen.findAllByTestId('chart')).toHaveLength(10)
    expect(document.querySelector('[data-chart="valuation"]')).toBeNull()
  })

  it('tones a falling share count as the gain it is', async () => {
    vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials())
    renderTab()
    await screen.findAllByTestId('chart')
    const cagr = within(card('shares')).getByText('-0.7%')
    expect(cagr.className).toMatch(/text-green-600/)
  })

  it('leaves a margin the company never files out of the legend', async () => {
    vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials({
      series: { gross_margin: fixtureSeries([null, null, null, null]) },
    }))
    renderTab()
    await screen.findAllByTestId('chart')
    const legend = within(card('margins')).getByRole('group', { name: 'Margins series' })
    expect(within(legend).getAllByRole('button').map((b) => b.textContent)).toEqual(['Operating62.4%', 'Net55.8%', 'FCF46.6%'])
    expect(within(card('margins')).getByTestId('headline').textContent).toBe('62.4%')
  })

  it('heads a card with the first series that has a value at that period', async () => {
    vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials({
      series: { gross_margin: fixtureSeries([0.99, 0.98, null, null]) },
    }))
    renderTab()
    await screen.findAllByTestId('chart')
    expect(within(card('margins')).getByTestId('headline').textContent).toBe('62.4%')
  })

  it('draws cash beside debt, and buybacks beside dividends paid', async () => {
    vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials())
    renderTab()
    await screen.findAllByTestId('chart')
    expect(card('cash-debt').textContent).toContain('Cash & debt')
    expect(card('cash-debt').textContent).toContain('Cash & equivalents$8.6B')
    expect(card('cash-debt').textContent).toContain('Debt$8.5B')
    expect(card('cash-debt').textContent).toContain('Net cash$34.8B')
    expect(card('cash-debt').textContent).not.toContain('CAGR')
    expect(within(card('capital-returns')).getByTestId('headline').textContent).toBe('$33.7B')
    expect(card('capital-returns').textContent).toContain('Dividends$834M')
  })

  it('hides the dividends card for a company that pays none', async () => {
    vi.spyOn(api, 'getCompanyFinancials').mockResolvedValue(fixtureFinancials({
      key_stats: { ...FIXTURE_KEY_STATS, dividends_per_share: null, payout_ratio: null },
      series: { dividends_per_share: fixtureSeries([null, null, null, null]) },
    }))
    renderTab()
    expect(await screen.findAllByTestId('chart')).toHaveLength(10)
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
    const [gross, operating, net, fcf] = within(legend).getAllByRole('button')
    fireEvent.click(gross)
    fireEvent.click(operating)
    fireEvent.click(net)
    expect(gross.getAttribute('aria-pressed')).toBe('false')
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
    expect(await screen.findAllByTestId('chart')).toHaveLength(11)
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
