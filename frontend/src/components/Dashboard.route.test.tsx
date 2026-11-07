// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Dashboard } from './Dashboard'

/**
 * The open tab lives in the URL's hash (`lib/tabRoute.ts`): a reload keeps it, a click
 * replaces it (no history entry per tab), and a company link pushes one.
 */

vi.mock('./PortfolioValueChart', () => ({ PortfolioValueChart: () => null }))

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

beforeEach(() => {
  localStorage.clear()
  // Every read stays pending: this is about which tab is open, not what it shows.
  vi.stubGlobal('fetch', () => new Promise(() => {}))
})

function renderDashboard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <Dashboard />
    </QueryClientProvider>,
  )
}

const selected = () =>
  screen.getAllByRole('tab').filter((t) => t.getAttribute('aria-selected') === 'true').map((t) => t.textContent)

describe('the Dashboard and the URL', () => {
  it('opens on Performance with a bare URL', () => {
    renderDashboard()
    expect(selected()).toEqual(['Performance'])
    expect(window.location.hash).toBe('')
  })

  it('opens the tab the URL names, as a reload does', () => {
    window.history.replaceState(null, '', '/#tax')
    renderDashboard()
    expect(selected()).toEqual(['Tax'])
  })

  it('opens the Company tab for a company link', () => {
    window.history.replaceState(null, '', '/#company/320193')
    renderDashboard()
    expect(selected()).toEqual(['Company'])
  })

  it('writes a tab click into the hash without a history entry per click', async () => {
    renderDashboard()
    const entries = window.history.length

    await userEvent.click(screen.getByRole('tab', { name: 'Dividends' }))
    expect(selected()).toEqual(['Dividends'])
    expect(window.location.hash).toBe('#dividends')

    await userEvent.click(screen.getByRole('tab', { name: 'Performance' }))
    expect(selected()).toEqual(['Performance'])
    expect(window.location.hash).toBe('')
    expect(window.history.length).toBe(entries)
  })

  it('follows the hash when it changes under the page, as Back does', () => {
    renderDashboard()
    act(() => {
      window.location.hash = '#watchlist'
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    expect(selected()).toEqual(['Watchlist'])
  })
})
