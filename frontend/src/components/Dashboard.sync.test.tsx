// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Dashboard } from './Dashboard'
import { api } from '@/lib/api'

/**
 * Issue #23. The statement a sync ingests writes positions, trades and cash transactions,
 * so every reader of those rows goes stale, not only `['portfolio']`: the dividend reads
 * sit on the Performance tab beside the positions.
 */

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

beforeEach(() => {
  localStorage.clear()
  if (!('ResizeObserver' in globalThis)) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
  }
  // Every read stays pending: this test is about which cache entries a sync marks stale.
  vi.stubGlobal('fetch', () => new Promise(() => {}))
})

type SyncResult = Awaited<ReturnType<typeof api.syncIBKRData>>

const year = new Date().getFullYear()
// Cached answers from before the sync, as a reader who visited these tabs has them.
const WRITTEN_BY_SYNC = [
  ['dividends', 'breakdown', year], // read on the Performance tab itself (YoC, yield cards)
  ['dividends', 'summary'], // DividendSummary, also on the Performance tab
  ['tax', 'report', year],
  ['allocation', 'portfolio'],
]
// Read by the page but independent of the statement: a sync must leave them alone.
const NOT_WRITTEN_BY_SYNC = [['settings'], ['health'], ['logos'], ['crypto', 'overview']]

/** Clicks Sync with the given outcome and waits until its `onSuccess` has run. */
async function syncWith(status: SyncResult['status']) {
  vi.spyOn(api, 'syncIBKRData').mockResolvedValue({ status } as SyncResult)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  for (const key of [...WRITTEN_BY_SYNC, ...NOT_WRITTEN_BY_SYNC]) {
    client.setQueryData(key, { cached: true })
  }
  render(
    <QueryClientProvider client={client}>
      <Dashboard />
    </QueryClientProvider>,
  )
  fireEvent.click(screen.getByRole('button', { name: 'Sync IBKR data' }))
  await waitFor(() =>
    expect(client.getMutationCache().getAll().map((m) => m.state.status)).toEqual(['success']),
  )
  return client
}

function invalidated(client: QueryClient, keys: unknown[][]) {
  return Object.fromEntries(
    keys.map((k) => [k.join('/'), Boolean(client.getQueryState(k)?.isInvalidated)]),
  )
}

const all = (keys: unknown[][], value: boolean) =>
  Object.fromEntries(keys.map((k) => [k.join('/'), value]))

describe('a manual IBKR sync that ingested a statement', () => {
  it('refreshes every reader of the rows it wrote, not only the portfolio keys', async () => {
    const client = await syncWith('success')
    const stale = Object.fromEntries(
      [...WRITTEN_BY_SYNC, ['portfolio', 'summary']].map((k) => {
        const s = client.getQueryState(k)
        return [k.join('/'), Boolean(s?.isInvalidated || s?.fetchStatus === 'fetching')]
      }),
    )
    expect(stale).toEqual(all([...WRITTEN_BY_SYNC, ['portfolio', 'summary']], true))
  })

  it('leaves settings, health, logos and the crypto book alone', async () => {
    const client = await syncWith('success')
    expect(invalidated(client, NOT_WRITTEN_BY_SYNC)).toEqual(all(NOT_WRITTEN_BY_SYNC, false))
  })
})

describe('a manual IBKR sync that was skipped', () => {
  it('refreshes nothing', async () => {
    const client = await syncWith('skipped')
    const keys = [...WRITTEN_BY_SYNC, ...NOT_WRITTEN_BY_SYNC, ['portfolio', 'summary']]
    expect(invalidated(client, keys)).toEqual(all(keys, false))
  })
})
