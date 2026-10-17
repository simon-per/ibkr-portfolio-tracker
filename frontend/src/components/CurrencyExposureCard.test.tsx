// @vitest-environment jsdom
import { describe, it, expect, afterEach, beforeEach } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { CurrencyExposureCard } from './CurrencyExposureCard'
import { CurrencyProvider } from '@/lib/CurrencyContext'
import type { CurrencyExposureInput } from '@/lib/currencyExposure'

/**
 * The arithmetic is pinned in `lib/currencyExposure.test.ts`. These cover what
 * only the component can get wrong: that the quote-currency caveat is actually
 * rendered rather than left in a comment, and that an outage is not turned into a
 * breakdown — the mistake the rebalance panel made and `e2e/errors` caught.
 *
 * With no backend the provider falls back to a base currency of EUR, which is
 * what the assertions below assume.
 */

afterEach(cleanup)
// Sections remember being opened (`useRememberedOpen`); each test starts closed.
beforeEach(() => localStorage.clear())

beforeEach(() => {
  if (!('ResizeObserver' in globalThis)) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
  }
})

function pos(
  security_id: number,
  symbol: string,
  currency: string | null,
  market_value_eur: number,
  market_price: number | null = 10,
): CurrencyExposureInput {
  return { security_id, symbol, currency, market_value_eur, market_price }
}

const book = [
  pos(1, 'AAPL', 'USD', 700),
  pos(2, 'SXR8', 'EUR', 200),
  pos(3, 'NESN', 'CHF', 100),
]

function withProviders(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <CurrencyProvider>{node}</CurrencyProvider>
    </QueryClientProvider>,
  )
}

function renderCard(
  positions: CurrencyExposureInput[] = book,
  fundSymbols?: ReadonlySet<string>,
  extra: { isError?: boolean } = {},
) {
  return withProviders(
    <CurrencyExposureCard positions={positions} fundSymbols={fundSymbols} {...extra} />,
  )
}

/**
 * Separate from `renderCard` on purpose: a default parameter swallows an
 * explicitly-passed `undefined`, so `renderCard(undefined)` silently rendered the
 * loaded book and the outage assertions passed against the wrong state.
 */
function renderUnloaded() {
  return withProviders(<CurrencyExposureCard positions={undefined} />)
}

function expand() {
  fireEvent.click(screen.getByRole('button', { name: /Currency Exposure/ }))
}

describe('CurrencyExposureCard', () => {
  it('leads with the share quoted outside the base currency', () => {
    renderCard()
    expect(screen.getByText(/80% of the book is quoted outside EUR/)).toBeTruthy()
  })

  it('starts collapsed', () => {
    renderCard()
    expect(screen.queryByRole('table')).toBeNull()
  })

  it('breaks the book down by quote currency, largest first', () => {
    renderCard()
    expand()
    const rows = screen.getAllByRole('row').slice(1) // drop the header
    expect(rows.map((r) => r.textContent?.match(/^[A-Z]{3}|^Unknown/)?.[0])).toEqual([
      'USD',
      'EUR',
      'CHF',
    ])
  })

  it('marks which row is the base currency', () => {
    renderCard()
    expand()
    expect(screen.getByText('base')).toBeTruthy()
  })

  describe('the quote-currency caveat', () => {
    it('is stated, with the share it applies to, when funds are held', () => {
      renderCard(book, new Set(['SXR8']))
      expand()
      expect(screen.getByText(/20.0% of the book sits in 1 fund,/)).toBeTruthy()
      expect(screen.getByText(/economic exposure can differ/)).toBeTruthy()
    })

    it('explains that the rows are not re-attributed', () => {
      // A reader who takes these rows as FX risk is wrong by the fund sleeve, so
      // the reason has to be on screen rather than in the source.
      renderCard(book, new Set(['SXR8']))
      expand()
      expect(screen.getByText(/counted in the currency they trade in/)).toBeTruthy()
    })

    it('is absent when nothing held is a fund', () => {
      renderCard(book)
      expand()
      expect(screen.queryByText(/economic exposure/)).toBeNull()
    })

    it('pluralises honestly', () => {
      renderCard(book, new Set(['SXR8', 'AAPL']))
      expand()
      expect(screen.getByText(/sits in 2 funds,/)).toBeTruthy()
    })
  })

  it('reports unpriced positions and that the total is understated', () => {
    renderCard([...book, pos(4, 'GHOST', 'CAD', 0, null)])
    expand()
    expect(screen.getByText(/1 position\(s\) have no cached price/)).toBeTruthy()
    expect(screen.getByText(/total is understated/)).toBeTruthy()
  })

  describe('when positions could not be loaded', () => {
    it('says so instead of reporting an exposure of zero', () => {
      renderUnloaded()
      expect(screen.getByText(/Couldn't load positions, so currency exposure/)).toBeTruthy()
      expect(screen.queryByText(/% of the book is quoted outside/)).toBeNull()
    })

    it('renders no table when expanded', () => {
      const { container } = renderUnloaded()
      expand()
      expect(container.querySelector('table')).toBeNull()
      expect(screen.getByText(/the server didn't respond/)).toBeTruthy()
    })

    it('treats an explicit query error the same as absent data', () => {
      renderCard([], undefined, { isError: true })
      expect(screen.getByText(/Couldn't load positions, so currency exposure/)).toBeTruthy()
    })

    it('distinguishes an empty portfolio from one that failed to load', () => {
      // An empty array is a real answer: nothing is held.
      renderCard([])
      expect(screen.queryByText(/Couldn't load positions/)).toBeNull()
      expect(screen.getByText(/Nothing priced yet/)).toBeTruthy()
    })

    it('says there is nothing to break down when every position is unpriced', () => {
      renderCard([pos(1, 'GHOST', 'CAD', 0, null)])
      expand()
      expect(screen.getByText(/No priced positions/)).toBeTruthy()
    })
  })
})
