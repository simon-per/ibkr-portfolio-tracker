// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { CompanyPeersCard } from './CompanyPeersCard'
import { api, type CompanyPeer, type CompanyPeers } from '@/lib/api'
import { FIXTURE_KEY_STATS } from '@/lib/companyFinancials.fixture'
import { CompanyLinksContext, companyLinks } from '@/lib/companyLinks'
import type { CompanyFinancialsCompany } from '@/lib/api'

function peer(cik: number, symbol: string, stats: Partial<CompanyPeer['key_stats']>): CompanyPeer {
  return {
    cik, security_id: null, symbol, name: `${symbol} Inc`, isin: null, held: false,
    currency: 'USD', key_stats: { ...FIXTURE_KEY_STATS, ...stats },
  }
}

const PEERS: CompanyPeers = {
  cik: 1, sic: 3674, industry: 'Semiconductors', widened: true,
  peers: [
    peer(1, 'NVDA', { pe_ratio: 45, revenue: 300e9 }),
    peer(2, 'AVGO', { pe_ratio: null, revenue: 60e9 }),
    peer(3, 'AMD', { pe_ratio: 90, revenue: 30e9 }),
  ],
}

// The tab's company list, which every peer is on: the links resolve through it.
const LINKS = companyLinks(
  PEERS.peers.map((p) => ({ ...p, security_ids: [] }) as unknown as CompanyFinancialsCompany),
)

function renderCard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <CompanyLinksContext.Provider value={LINKS}>
        <CompanyPeersCard cik={1} industry="Semiconductors" />
      </CompanyLinksContext.Provider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('CompanyPeersCard (review item R1)', () => {
  it('asks the server only once it is opened', async () => {
    const get = vi.spyOn(api, 'getCompanyPeers').mockResolvedValue(PEERS)
    renderCard()
    expect(screen.getByText('Semiconductors')).toBeTruthy()
    expect(get).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: /Peers/ }))
    await screen.findByTestId('company-peers')
    await waitFor(() => expect(get).toHaveBeenCalledWith(1))
    // The three-digit group is named as such.
    expect(await screen.findByText('SIC 367x')).toBeTruthy()
  })

  it('leads with the company, sorts a figure with its blanks last and links each row', async () => {
    vi.spyOn(api, 'getCompanyPeers').mockResolvedValue(PEERS)
    renderCard()
    fireEvent.click(screen.getByRole('button', { name: /Peers/ }))
    const table = await screen.findByRole('table')
    const symbols = () =>
      within(table).getAllByRole('link').map((a) => a.querySelector('.font-medium')?.textContent)
    expect(symbols()).toEqual(['NVDA', 'AVGO', 'AMD'])

    fireEvent.click(within(table).getByRole('button', { name: /P\/E/ }))
    // Highest first, AVGO's absent P/E last.
    expect(symbols()).toEqual(['AMD', 'NVDA', 'AVGO'])
    expect(within(table).getAllByRole('link')[0].getAttribute('href')).toBe('#company/3')
  })
})
