// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider, useQueries } from '@tanstack/react-query'
import { useFormatCurrency } from './CurrencyContext'
import { dataOf } from './queryCache'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { formatCount, formatCurrency, formatMoneyWhole, formatPercent } from './utils'
import { ScrollableTable } from '@/components/ui/ScrollableTable'

/**
 * Issue #48: identities that changed on a render that changed nothing, so every memo built
 * on them recomputed, and work repeated on every render that only needs doing once.
 */

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('useQueries with the `dataOf` combine', () => {
  it('returns the same array when nothing changed between renders', async () => {
    const client = new QueryClient()
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    )
    // Dashboard.tsx's benchmark queries, minus the API.
    const { result, rerender } = renderHook(
      () =>
        useQueries({
          queries: ['sp500'].map((k) => ({ queryKey: ['b', k], queryFn: async () => k })),
          combine: dataOf,
        }),
      { wrapper },
    )
    await waitFor(() => expect(result.current[0]).toBe('sp500'))
    const before = result.current
    rerender() // e.g. the 60 s scheduler poll re-rendering the Dashboard
    // `benchmarkDatasets` and `riskMetrics` are memoised on this array.
    expect(result.current).toBe(before)
  })
})

describe('useFormatCurrency', () => {
  it('returns the same formatter while the base currency is unchanged', () => {
    const { result, rerender } = renderHook(() => useFormatCurrency())
    const before = result.current
    rerender()
    // PositionsList/DividendsTab/LookThroughTab/WatchlistTab memoise their columns on it.
    expect(result.current).toBe(before)
  })
})

describe('the number formatters', () => {
  it('build one Intl.NumberFormat per option set, not one per call', () => {
    const Real = Intl.NumberFormat
    // A function returning a real formatter: `new` on it yields that object, prototype intact.
    const construct = vi
      .spyOn(Intl, 'NumberFormat')
      .mockImplementation(function (...args: ConstructorParameters<typeof Intl.NumberFormat>) {
        return new Real(...args)
      } as unknown as typeof Intl.NumberFormat)
    // A currency no other test in this file formats, so its formatter is not cached yet.
    formatCurrency(1, 'JPY')
    formatCurrency(2, 'JPY')
    formatCurrency(3, 'JPY')
    expect(construct).toHaveBeenCalledTimes(1)
    formatCount(4)
    formatCount(5)
    formatPercent(6)
    formatPercent(7)
    expect(construct).toHaveBeenCalledTimes(3)
    // Whole units too: it built a new formatter per call until 2026-10-18 (#70).
    formatMoneyWhole(8, 'JPY')
    formatMoneyWhole(9, 'JPY')
    expect(construct).toHaveBeenCalledTimes(4)
    // And the cached formatter is the one each call would have built.
    expect(formatCurrency(1234.5, 'JPY')).toBe(
      new Intl.NumberFormat('en-US', {
        style: 'currency',
        currency: 'JPY',
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      }).format(1234.5),
    )
  })

  it('are built only by the cache: utils.ts constructs Intl.NumberFormat in one place', () => {
    const source = readFileSync(join(__dirname, 'utils.ts'), 'utf8')
    expect(source.match(/new Intl\.NumberFormat\(/g)).toHaveLength(1)
  })
})

describe('ScrollableTable', () => {
  it('attaches its observer once per mount, not once per parent render', () => {
    let constructed = 0
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor() {
          constructed += 1
        }
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    )
    const table = (n: number) => (
      <ScrollableTable label="Test table">
        <table>
          <tbody>
            <tr>
              <td>{n}</td>
            </tr>
          </tbody>
        </table>
      </ScrollableTable>
    )
    const { rerender } = render(table(1))
    // A parent render hands over a new `children` element each time.
    rerender(table(2))
    rerender(table(3))
    expect(constructed).toBe(1)
  })

  it('observes a child the parent swaps in after mount', async () => {
    const observed: Element[] = []
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe(el: Element) {
          observed.push(el)
        }
        unobserve() {}
        disconnect() {}
      },
    )
    const { rerender } = render(
      <ScrollableTable label="Test table">
        <p>Loading</p>
      </ScrollableTable>,
    )
    rerender(
      <ScrollableTable label="Test table">
        <table data-testid="arrived" />
      </ScrollableTable>,
    )
    // The ResizeObserver watches the children for "the data arrived and widened the
    // table"; a replaced child would otherwise go unwatched.
    await waitFor(() =>
      expect(observed.some((el) => el.getAttribute('data-testid') === 'arrived')).toBe(true),
    )
  })
})
