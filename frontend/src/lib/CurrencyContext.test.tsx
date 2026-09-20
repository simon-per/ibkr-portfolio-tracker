// @vitest-environment jsdom
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { render, screen, cleanup, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  CurrencyProvider,
  useBaseCurrency,
  useCurrencySymbol,
  useFormatCurrency,
} from './CurrencyContext'
import { api } from './api'

/**
 * This context decides the label on every money figure in the app, and it had no
 * tests.
 *
 * The bug they were written for: `/api/settings` is fetched with
 * `staleTime: Infinity` because the value only changes through our own mutation —
 * so a single failed fetch is not a blip but sticky for the whole session. The
 * provider fell back to `'EUR'` and said nothing, which meant every figure was
 * labelled `€` while the numbers behind them were whatever the account actually
 * uses. The figures were right; the label was wrong, which is worse than a gap.
 */

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

beforeEach(() => {
  vi.spyOn(console, 'error').mockImplementation(() => {})
})

function Probe() {
  const { baseCurrency, currencyIsAssumed, updateError, isUpdating } = useBaseCurrency()
  const format = useFormatCurrency()
  const symbol = useCurrencySymbol()
  return (
    <dl>
      <dd data-testid="base">{baseCurrency}</dd>
      <dd data-testid="assumed">{String(currencyIsAssumed)}</dd>
      <dd data-testid="updateError">{updateError ?? '(none)'}</dd>
      <dd data-testid="isUpdating">{String(isUpdating)}</dd>
      <dd data-testid="formatted">{format(1234.5)}</dd>
      <dd data-testid="symbol">{symbol}</dd>
    </dl>
  )
}

function renderProbe() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const result = render(
    <QueryClientProvider client={client}>
      <CurrencyProvider>
        <Probe />
      </CurrencyProvider>
    </QueryClientProvider>,
  )
  return { ...result, client }
}

const CHF_SETTINGS = {
  base_currency: 'CHF',
  supported_currencies: ['EUR', 'CHF', 'USD'],
  dividend_forecast_withholding_pct: 15,
}

function read(id: string): string {
  return screen.getByTestId(id).textContent ?? ''
}

describe('CurrencyProvider', () => {
  it('uses the account currency once settings load', async () => {
    vi.spyOn(api, 'getSettings').mockResolvedValue(CHF_SETTINGS as never)
    renderProbe()
    await waitFor(() => expect(read('base')).toBe('CHF'))
    expect(read('assumed')).toBe('false')
  })

  it('binds the formatter and symbol to that currency', async () => {
    vi.spyOn(api, 'getSettings').mockResolvedValue(CHF_SETTINGS as never)
    renderProbe()
    await waitFor(() => expect(read('base')).toBe('CHF'))
    // The whole point of the context: one source for the label.
    expect(read('formatted')).toContain('CHF')
    expect(read('symbol')).toBe('CHF ')
  })

  describe('when the settings fetch fails', () => {
    beforeEach(() => {
      vi.spyOn(api, 'getSettings').mockRejectedValue(new Error('boom'))
    })

    it('says the currency is assumed rather than asserting EUR', async () => {
      renderProbe()
      await waitFor(() => expect(read('assumed')).toBe('true'))
      expect(read('base')).toBe('EUR')
    })

    it('still supplies a usable formatter, since a blank figure is worse', async () => {
      renderProbe()
      await waitFor(() => expect(read('assumed')).toBe('true'))
      expect(read('formatted')).toBe('€1,234.50')
    })
  })

  it('does not claim the currency is assumed while the fetch is in flight', async () => {
    // A pending fallback is a placeholder, not a claim. Alarming on first paint
    // would train the reader to ignore the warning.
    let resolve: (v: unknown) => void = () => {}
    vi.spyOn(api, 'getSettings').mockReturnValue(
      new Promise((r) => { resolve = r }) as never,
    )
    renderProbe()
    expect(read('assumed')).toBe('false')
    expect(read('base')).toBe('EUR')
    await act(async () => { resolve(CHF_SETTINGS) })
    await waitFor(() => expect(read('base')).toBe('CHF'))
    expect(read('assumed')).toBe('false')
  })

  describe('changing the currency', () => {
    it('writes the new settings and invalidates everything', async () => {
      // Every money figure depends on the base currency, so a partial
      // invalidation would leave surfaces disagreeing with the label.
      const USD_SETTINGS = {
        base_currency: 'USD',
        supported_currencies: ['EUR', 'CHF', 'USD'],
        dividend_forecast_withholding_pct: 15,
      }
      // `invalidateQueries()` invalidates *everything*, `['settings']` included, so
      // the optimistic `setQueryData` is immediately overwritten by a refetch. The
      // server's answer therefore wins — which is right, because the value was just
      // persisted there. The mock has to change with it or it would assert that a
      // stale server reply is honoured.
      let stored: unknown = CHF_SETTINGS
      vi.spyOn(api, 'getSettings').mockImplementation(async () => stored as never)
      const update = vi.spyOn(api, 'updateBaseCurrency').mockImplementation(async () => {
        stored = USD_SETTINGS
        return USD_SETTINGS as never
      })

      function Changer() {
        const { setBaseCurrency } = useBaseCurrency()
        return <button onClick={() => setBaseCurrency('USD')}>change</button>
      }

      const client = new QueryClient({
        defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
      })
      const invalidate = vi.spyOn(client, 'invalidateQueries')
      render(
        <QueryClientProvider client={client}>
          <CurrencyProvider><Probe /><Changer /></CurrencyProvider>
        </QueryClientProvider>,
      )
      await waitFor(() => expect(read('base')).toBe('CHF'))

      await act(async () => { screen.getByText('change').click() })
      await waitFor(() => expect(read('base')).toBe('USD'))
      expect(update).toHaveBeenCalledWith('USD')
      expect(invalidate).toHaveBeenCalled()
    })

    it('reports a failed change and keeps showing the previous currency', async () => {
      vi.spyOn(api, 'getSettings').mockResolvedValue(CHF_SETTINGS as never)
      vi.spyOn(api, 'updateBaseCurrency').mockRejectedValue(new Error('nope'))

      function Changer() {
        const { setBaseCurrency } = useBaseCurrency()
        return <button onClick={() => setBaseCurrency('USD')}>change</button>
      }

      render(
        <QueryClientProvider
          client={new QueryClient({
            defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
          })}
        >
          <CurrencyProvider><Probe /><Changer /></CurrencyProvider>
        </QueryClientProvider>,
      )
      await waitFor(() => expect(read('base')).toBe('CHF'))
      await act(async () => { screen.getByText('change').click() })

      await waitFor(() => expect(read('updateError')).toMatch(/Currency change failed/))
      // The dropdown would otherwise silently revert with no explanation.
      expect(read('base')).toBe('CHF')
      // A failed *change* is not the same as an unknown currency.
      expect(read('assumed')).toBe('false')
    })
  })

  it('falls back to the supported list when settings do not supply one', async () => {
    vi.spyOn(api, 'getSettings').mockResolvedValue({ base_currency: 'USD' } as never)

    function Supported() {
      const { supportedCurrencies } = useBaseCurrency()
      return <span data-testid="supported">{supportedCurrencies.join(',')}</span>
    }
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <CurrencyProvider><Supported /></CurrencyProvider>
      </QueryClientProvider>,
    )
    await waitFor(() =>
      expect(screen.getByTestId('supported').textContent).toBe('EUR,CHF,USD'),
    )
  })
})

describe('useFormatCurrency', () => {
  it('accepts an override for a figure that is not in the base currency', async () => {
    vi.spyOn(api, 'getSettings').mockResolvedValue(CHF_SETTINGS as never)

    function Override() {
      const format = useFormatCurrency()
      return <span data-testid="over">{format(1000, 'USD')}</span>
    }
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <CurrencyProvider><Override /></CurrencyProvider>
      </QueryClientProvider>,
    )
    await waitFor(() => expect(screen.getByTestId('over').textContent).toBe('$1,000.00'))
  })
})
