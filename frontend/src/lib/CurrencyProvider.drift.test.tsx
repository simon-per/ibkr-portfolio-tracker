// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useBaseCurrency } from './CurrencyContext'
import { CurrencyProvider } from './CurrencyProvider'
import { api } from './api'

/**
 * Review item C3. The settings are read once a session (`staleTime: Infinity`); when the
 * base currency changed elsewhere, every response arrived in the new one while the label
 * stayed on the old. A response served in another base than the cached settings name now
 * refreshes them, and resets what was converted into the old one.
 */

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const settings = (base: string) => ({
  base_currency: base,
  supported_currencies: ['EUR', 'CHF', 'USD'],
  dividend_forecast_withholding_pct: 15,
})

function Label() {
  const { baseCurrency } = useBaseCurrency()
  return <p data-testid="label">{baseCurrency}</p>
}

function mount(client: QueryClient) {
  render(
    <QueryClientProvider client={client}>
      <CurrencyProvider>
        <Label />
      </CurrencyProvider>
    </QueryClientProvider>,
  )
}

describe('a response served in another base currency', () => {
  it('is caught when it arrived before the settings did', async () => {
    let serverBase = 'CHF'
    let answer: (() => void) | null = null
    vi.spyOn(api, 'getSettings').mockImplementation(() => new Promise((resolve) => {
      const reply = settings(serverBase)
      if (answer === null) answer = () => resolve({ ...settings('EUR') } as never)
      else resolve(reply as never)
    }))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    mount(client)
    client.setQueryData(['portfolio', 'summary'], { base_currency: 'CHF', total: 95 })
    // The first settings answer is the stale EUR one.
    answer!()
    serverBase = 'CHF'
    await waitFor(() => expect(screen.getByTestId('label').textContent).toBe('CHF'))
  })

  it('refreshes the settings and resets the figures converted into the old one', async () => {
    let serverBase = 'EUR'
    const getSettings = vi.spyOn(api, 'getSettings')
      .mockImplementation(async () => settings(serverBase) as never)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    mount(client)
    await waitFor(() => expect(client.getQueryData(['settings'])).toBeDefined())
    client.setQueryData(['dividends', 'summary'], { base_currency: 'EUR', total: 1 })

    // Switched on another device: the next response is in CHF.
    serverBase = 'CHF'
    client.setQueryData(['portfolio', 'summary'], { base_currency: 'CHF', total: 95 })

    await waitFor(() => expect(screen.getByTestId('label').textContent).toBe('CHF'))
    expect(getSettings).toHaveBeenCalledTimes(2)
    // The EUR figure cached before the switch is gone, not relabelled.
    await waitFor(() => expect(client.getQueryData(['dividends', 'summary'])).toBeUndefined())
  })

  it('asks once, and leaves everything alone when the settings were right', async () => {
    const getSettings = vi.spyOn(api, 'getSettings')
      .mockImplementation(async () => settings('EUR') as never)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    mount(client)
    await waitFor(() => expect(client.getQueryData(['settings'])).toBeDefined())
    client.setQueryData(['dividends', 'summary'], { base_currency: 'EUR', total: 1 })

    // A stray answer in CHF (a request in flight across a switch here and back).
    client.setQueryData(['portfolio', 'summary'], { base_currency: 'CHF', total: 95 })
    client.setQueryData(['portfolio', 'positions-like'], { base_currency: 'CHF', total: 2 })
    await waitFor(() => expect(getSettings).toHaveBeenCalledTimes(2))
    await new Promise((r) => setTimeout(r, 20))
    expect(getSettings).toHaveBeenCalledTimes(2)
    expect(screen.getByTestId('label').textContent).toBe('EUR')
    expect(client.getQueryData(['dividends', 'summary'])).toEqual({ base_currency: 'EUR', total: 1 })
  })
})
