// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Dashboard } from './Dashboard'
import { api } from '@/lib/api'
import { BENCHMARKS_KEY } from '@/lib/benchmarkSelection'

/**
 * Issue #48. What re-renders the Dashboard, and what a re-render recomputes. The value
 * chart stands in for the Performance tab: it counts Dashboard renders and records the
 * `benchmarks` array it was handed.
 */

const realScrollY = Object.getOwnPropertyDescriptor(window, 'scrollY')

const chartRenders = vi.hoisted(() => [] as unknown[])
vi.mock('./PortfolioValueChart', () => ({
  PortfolioValueChart: ({ benchmarks }: { benchmarks: unknown }) => {
    chartRenders.push(benchmarks)
    return null
  },
}))

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  if (realScrollY) Object.defineProperty(window, 'scrollY', realScrollY)
})

beforeEach(() => {
  chartRenders.length = 0
  localStorage.clear()
  localStorage.setItem(BENCHMARKS_KEY, JSON.stringify(['sp500']))
  if (!('ResizeObserver' in globalThis)) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
  }
  // Every other read stays pending; only the benchmark answers.
  vi.stubGlobal('fetch', () => new Promise(() => {}))
  vi.spyOn(api, 'getBenchmarkComparison').mockResolvedValue({
    benchmark_name: 'S&P 500',
    data: [],
  } as unknown as Awaited<ReturnType<typeof api.getBenchmarkComparison>>)
})

function page(client: QueryClient) {
  return (
    <QueryClientProvider client={client}>
      <Dashboard />
    </QueryClientProvider>
  )
}

async function renderWithBenchmark() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(page(client))
  await waitFor(() => expect(chartRenders.at(-1)).toHaveLength(1))
  return { ...view, client }
}

describe('Dashboard renders', () => {
  it('crossing the scroll threshold re-renders the section strip, not the page', async () => {
    await renderWithBenchmark()
    const strip = screen.getByRole('tablist', { name: 'Portfolio sections' }).parentElement!
    expect(strip.className).not.toContain('shadow-[')
    const before = chartRenders.length

    Object.defineProperty(window, 'scrollY', { value: 200, configurable: true })
    act(() => {
      fireEvent.scroll(window)
    })

    expect(strip.className).toContain('shadow-[')
    expect(chartRenders.length).toBe(before)
  })

  it('hands the chart the same benchmarks across a render that changed nothing', async () => {
    const { client, rerender } = await renderWithBenchmark()
    const before = chartRenders.at(-1)
    const count = chartRenders.length

    rerender(page(client)) // a parent render, as the 60 s scheduler poll causes

    expect(chartRenders.length).toBeGreaterThan(count)
    expect(chartRenders.at(-1)).toBe(before)
  })
})
