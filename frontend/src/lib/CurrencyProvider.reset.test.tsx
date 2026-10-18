// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { useBaseCurrency, useFormatCurrency } from './CurrencyContext'
import { CurrencyProvider } from './CurrencyProvider'
import { api } from './api'

/**
 * Issue #22. No query key carries the base currency, so a switch that only invalidated
 * left every cached figure on screen under the new label until its refetch landed: EUR
 * amounts printed as CHF. The switch resets the cache instead (`dependsOnBaseCurrency`).
 */

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})
beforeEach(() => {
  vi.spyOn(console, 'error').mockImplementation(() => {})
})

const settings = (base: string) => ({
  base_currency: base,
  supported_currencies: ['EUR', 'CHF', 'USD'],
  dividend_forecast_withholding_pct: 15,
})

/** A money endpoint: the figure, and the currency the server converted it into. */
type Money = { base_currency: string; total: number }
let serverBase = 'EUR'
let release: (() => void) | null = null
const fetchMoney = vi.fn(
  () =>
    new Promise<Money>((resolve) => {
      const answer = { base_currency: serverBase, total: serverBase === 'EUR' ? 100 : 95 }
      // The first fetch answers at once; a refetch waits until the test releases it.
      if (fetchMoney.mock.calls.length === 1) resolve(answer)
      else release = () => resolve(answer)
    }),
)

function MoneyProbe() {
  const format = useFormatCurrency()
  const { data } = useQuery({ queryKey: ['portfolio', 'summary'], queryFn: fetchMoney })
  if (!data) return <p data-testid="money">loading</p>
  return (
    <p data-testid="money" data-served-in={data.base_currency}>
      {format(data.total)}
    </p>
  )
}

/** The currency the shown figure is labelled in, or null while nothing is shown. */
function labelledAs(el: HTMLElement): string | null {
  const text = el.textContent ?? ''
  if (text.includes('CHF')) return 'CHF'
  if (text.includes('€')) return 'EUR'
  return null
}

function Switch() {
  const { setBaseCurrency } = useBaseCurrency()
  return <button onClick={() => setBaseCurrency('CHF')}>to CHF</button>
}

describe('switching the base currency', () => {
  it('never labels a figure with a currency other than the one it was served in', async () => {
    serverBase = 'EUR'
    vi.spyOn(api, 'getSettings').mockImplementation(async () => settings(serverBase) as never)
    vi.spyOn(api, 'updateBaseCurrency').mockImplementation(async () => {
      serverBase = 'CHF'
      return settings('CHF') as never
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <CurrencyProvider>
          <MoneyProbe />
          <Switch />
        </CurrencyProvider>
      </QueryClientProvider>,
    )
    await waitFor(() => expect(screen.getByTestId('money').textContent).toBe('€100.00'))

    await act(async () => screen.getByText('to CHF').click())
    // The mutation has resolved; the money refetch is still in flight.
    await waitFor(() => expect(fetchMoney).toHaveBeenCalledTimes(2))

    await waitFor(() => expect(client.getQueryData<{ base_currency: string }>(['settings'])?.base_currency).toBe('CHF'))
    const el = screen.getByTestId('money')
    // What the reader sees while the refetch runs.
    expect(labelledAs(el)).toBe(el.dataset.servedIn ?? null)

    await act(async () => release?.())
  })
})

describe('a tab opened after the switch', () => {
  it('does not show its cached figures under the new label', async () => {
    serverBase = 'EUR'
    fetchMoney.mockClear()
    vi.spyOn(api, 'getSettings').mockImplementation(async () => settings(serverBase) as never)
    vi.spyOn(api, 'updateBaseCurrency').mockImplementation(async () => {
      serverBase = 'CHF'
      return settings('CHF') as never
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const tree = (showTab: boolean) => (
      <QueryClientProvider client={client}>
        <CurrencyProvider>
          {showTab && <MoneyProbe />}
          <Switch />
        </CurrencyProvider>
      </QueryClientProvider>
    )
    const { rerender } = render(tree(true))
    await waitFor(() => expect(screen.getByTestId('money').textContent).toBe('€100.00'))
    rerender(tree(false)) // the reader moves to another tab; the query is now inactive

    await act(async () => screen.getByText('to CHF').click())
    await waitFor(() => expect(client.getQueryData<{ base_currency: string }>(['settings'])?.base_currency).toBe('CHF'))

    rerender(tree(true)) // back to the tab: its refetch is in flight
    const el = screen.getByTestId('money')
    // The label must name the currency the figure was served in.
    expect(labelledAs(el)).toBe(el.dataset.servedIn ?? null)
    await act(async () => release?.())
  })
})

describe('a refetch that fails after the switch', () => {
  it('leaves an error state, not the old-currency figure', async () => {
    serverBase = 'EUR'
    vi.spyOn(api, 'getSettings').mockImplementation(async () => settings(serverBase) as never)
    vi.spyOn(api, 'updateBaseCurrency').mockImplementation(async () => {
      serverBase = 'CHF'
      return settings('CHF') as never
    })
    const failing = vi.fn(async (): Promise<Money> => {
      if (failing.mock.calls.length === 1) return { base_currency: 'EUR', total: 100 }
      throw new Error('upstream down')
    })
    function FailingProbe() {
      const format = useFormatCurrency()
      const { data, isError } = useQuery({ queryKey: ['portfolio', 'positions'], queryFn: failing })
      if (isError) return <p data-testid="money">error</p>
      return <p data-testid="money">{data ? format(data.total) : 'loading'}</p>
    }
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <CurrencyProvider>
          <FailingProbe />
          <Switch />
        </CurrencyProvider>
      </QueryClientProvider>,
    )
    await waitFor(() => expect(screen.getByTestId('money').textContent).toBe('€100.00'))

    await act(async () => screen.getByText('to CHF').click())
    await waitFor(() => expect(screen.getByTestId('money').textContent).toBe('error'))
    expect(client.getQueryData(['portfolio', 'positions'])).toBeUndefined()
  })
})
