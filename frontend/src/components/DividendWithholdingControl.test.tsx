// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { DividendWithholdingControl } from './DividendWithholdingControl'
import { api } from '@/lib/api'

/**
 * Review items T6 and U5. An estimated dividend's net is its gross less the assumed rate,
 * and the dividends were not its only reader: the Tax tab's projection, XIRR's dividend
 * inflows and the activity ledger kept the old rate for their stale time.
 */

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

beforeEach(() => {
  // Every refetch stays pending: this is about which cache entries the save marks stale.
  vi.stubGlobal('fetch', () => new Promise(() => {}))
})

const year = new Date().getFullYear()
const READ_THE_RATE = [
  ['dividends', 'breakdown', year],
  ['dividends', 'summary'],
  ['dividends', 'forecast-history'],
  ['tax', 'report', year],
  ['portfolio', 'annualized-return', { start: '2026-01-01', end: '2026-11-08' }],
  ['portfolio', 'activity', { limit: 50, offset: 0 }],
]
const DO_NOT = [
  ['portfolio', 'summary'],
  ['portfolio', 'positions'],
  ['portfolio', 'value-over-time', { start: '2026-01-01', end: '2026-11-08' }],
  ['portfolio', 'contributions'],
  ['performance', 'closed-positions'],
  ['fundamentals', 'portfolio'],
  ['watchlist'],
  ['crypto', 'portfolio'],
  ['health'],
]

describe('saving a new forecast withholding rate', () => {
  it('refreshes every reader of an estimated net and nothing else', async () => {
    vi.spyOn(api, 'updateDividendWithholding').mockResolvedValue(
      { dividend_forecast_withholding_pct: 20 } as unknown as Awaited<
        ReturnType<typeof api.updateDividendWithholding>
      >,
    )
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    for (const key of [...READ_THE_RATE, ...DO_NOT]) client.setQueryData(key, { cached: true })
    render(
      <QueryClientProvider client={client}>
        <DividendWithholdingControl appliedPct={15} />
      </QueryClientProvider>,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Edit forecast withholding' }))
    fireEvent.change(await screen.findByLabelText(/withholding/i, { selector: 'input' }), {
      target: { value: '20' },
    })
    fireEvent.submit(screen.getByLabelText(/withholding/i, { selector: 'input' }).closest('form')!)
    await waitFor(() =>
      expect(client.getMutationCache().getAll().map((m) => m.state.status)).toEqual(['success']),
    )

    const stale = (keys: unknown[][]) =>
      Object.fromEntries(keys.map((k) => [JSON.stringify(k), Boolean(client.getQueryState(k)?.isInvalidated)]))
    const every = (keys: unknown[][], value: boolean) =>
      Object.fromEntries(keys.map((k) => [JSON.stringify(k), value]))
    expect(stale(READ_THE_RATE)).toEqual(every(READ_THE_RATE, true))
    expect(stale(DO_NOT)).toEqual(every(DO_NOT, false))
    expect(client.getQueryData(['settings'])).toEqual({ dividend_forecast_withholding_pct: 20 })
  })
})
