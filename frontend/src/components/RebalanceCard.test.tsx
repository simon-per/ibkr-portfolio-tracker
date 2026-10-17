// @vitest-environment jsdom
import { describe, it, expect, afterEach, beforeEach } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RebalanceCard } from './RebalanceCard'
import { readTargets } from '@/lib/rebalanceTargets'
import { CurrencyProvider } from '@/lib/CurrencyProvider'
import type { DriftInput } from '@/lib/rebalance'

/**
 * The arithmetic is pinned in `lib/rebalance.test.ts`. These cover what only the
 * component can get wrong: that clearing a field removes a target instead of
 * storing 0, that the caveats which ride on a successful render are actually
 * rendered, and that a corrupt localStorage value cannot take the tab down.
 */

afterEach(cleanup)
beforeEach(() => localStorage.clear())

/**
 * `ScrollableTable` measures overflow with a ResizeObserver, which jsdom does not
 * implement. It has been in every browser since 2020, so this is a gap in the
 * test environment rather than something the component should guard against.
 */
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
  market_value_eur: number,
  market_price: number | null = 10,
): DriftInput {
  return { security_id, symbol, description: `${symbol} Inc`, market_value_eur, market_price }
}

const equalFour = [pos(1, 'AAA', 250), pos(2, 'BBB', 250), pos(3, 'CCC', 250), pos(4, 'DDD', 250)]

/**
 * `CurrencyProvider` reads the base currency through TanStack Query, so it needs
 * a client above it. Retries off and no network: the provider falls back to its
 * default currency, which is all these assertions need.
 */
function withProviders(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <CurrencyProvider>{node}</CurrencyProvider>
    </QueryClientProvider>,
  )
}

function renderCard(positions: DriftInput[] = equalFour) {
  return withProviders(<RebalanceCard positions={positions} />)
}

function targetInput(symbol: string): HTMLInputElement {
  return screen.getByLabelText(`Target weight for ${symbol}`) as HTMLInputElement
}

describe('RebalanceCard', () => {
  it('starts collapsed with no targets, and says how to begin', () => {
    renderCard()
    expect(screen.getByText(/No targets set/)).toBeTruthy()
    expect(screen.queryByLabelText('Target weight for AAA')).toBeNull()
  })

  it('opens already expanded when targets exist in this browser', () => {
    localStorage.setItem('allocationTargets', JSON.stringify({ 1: 25 }))
    renderCard()
    expect(targetInput('AAA').value).toBe('25')
  })

  describe('clearing a field', () => {
    beforeEach(() => {
      localStorage.setItem('allocationTargets', JSON.stringify({ 1: 40, 2: 20, 3: 20, 4: 20 }))
    })

    it('removes the target rather than storing zero', () => {
      // Storing 0 would mean "hold none of this" and advise selling the lot.
      renderCard()
      fireEvent.change(targetInput('AAA'), { target: { value: '' } })
      expect(readTargets()).toEqual({ 2: 20, 3: 20, 4: 20 })
    })

    it('leaves the position unadvised afterwards', () => {
      renderCard()
      fireEvent.change(targetInput('AAA'), { target: { value: '' } })
      const row = targetInput('AAA').closest('tr')!
      // Drift and trade columns both blank, not a number.
      expect(row.textContent).toContain('—')
      expect(row.textContent).not.toContain('pp')
    })
  })

  it('states that targets do not sum to 100 instead of scaling them', () => {
    localStorage.setItem('allocationTargets', JSON.stringify({ 1: 30, 2: 30 }))
    renderCard()
    expect(screen.getByText(/Targets sum to 60.0%, not 100%/)).toBeTruthy()
    expect(screen.getByText(/not net to zero/)).toBeTruthy()
    // The stated target is untouched.
    expect(targetInput('AAA').value).toBe('30')
  })

  it('says when more is targeted than exists', () => {
    localStorage.setItem('allocationTargets', JSON.stringify({ 1: 60, 2: 60 }))
    renderCard()
    expect(screen.getByText(/More is targeted than exists/)).toBeTruthy()
  })

  it('reports unpriced positions and that the total is understated', () => {
    localStorage.setItem('allocationTargets', JSON.stringify({ 1: 50 }))
    renderCard([pos(1, 'AAA', 500), pos(2, 'GHOST', 0, null)])
    expect(screen.getByText(/no cached price/)).toBeTruthy()
    expect(screen.getByText(/total is understated/)).toBeTruthy()
  })

  it('names the untargeted share separately from the target shortfall', () => {
    // These are different quantities — one in target space, one in current-weight
    // space — and they happen to coincide here, so the wording must not collide.
    localStorage.setItem('allocationTargets', JSON.stringify({ 1: 25, 2: 25 }))
    renderCard()
    expect(screen.getByText(/current value has no target/)).toBeTruthy()
    expect(screen.getByText(/Targets sum to 50.0%, not 100%/)).toBeTruthy()
  })

  describe('when positions could not be loaded', () => {
    // Regression: with the backend down and targets saved, the panel drew a whole
    // plan out of the outage — every target became an "unheld" row reading "Not
    // currently held", under a header claiming 0 positions outside the band. Two
    // confident falsehoods about the portfolio.
    beforeEach(() => {
      localStorage.setItem('allocationTargets', JSON.stringify({ 1: 30, 2: 25, 3: 20 }))
    })

    function renderUnloaded() {
      return withProviders(<RebalanceCard positions={undefined} />)
    }

    it('says so instead of claiming nothing is outside the band', () => {
      renderUnloaded()
      expect(screen.getByText(/Couldn't load positions/)).toBeTruthy()
      expect(screen.queryByText(/outside the/)).toBeNull()
      expect(screen.queryByText(/Nothing to do/)).toBeNull()
    })

    it('does not describe held positions as not currently held', () => {
      renderUnloaded()
      expect(screen.queryByText('Not currently held')).toBeNull()
    })

    it('renders no table at all rather than one built from an empty list', () => {
      const { container } = renderUnloaded()
      expect(container.querySelector('table')).toBeNull()
    })

    it('reassures that the saved targets survive the outage', () => {
      renderUnloaded()
      expect(screen.getByText(/saved targets are untouched/)).toBeTruthy()
      expect(readTargets()).toEqual({ 1: 30, 2: 25, 3: 20 })
    })

    it('treats an explicit query error the same as absent data', () => {
      withProviders(<RebalanceCard positions={[]} isError />)
      expect(screen.getByText(/Couldn't load positions/)).toBeTruthy()
    })

    it('distinguishes an empty portfolio from one that failed to load', () => {
      // An empty array is "none held", which is a real answer and not an error.
      withProviders(<RebalanceCard positions={[]} />)
      expect(screen.queryByText(/Couldn't load positions/)).toBeNull()
    })
  })

  it('says nothing could be compared when no target is judgeable', () => {
    // Positions loaded fine, but every target sits on an unpriced holding — so
    // "0 outside the band" would again read as an all-clear.
    localStorage.setItem('allocationTargets', JSON.stringify({ 1: 100 }))
    renderCard([pos(1, 'GHOST', 0, null)])
    expect(screen.getByText(/No target could be compared/)).toBeTruthy()
  })

  it('reports nothing to do when every target is inside the band', () => {
    localStorage.setItem('allocationTargets', JSON.stringify({ 1: 25, 2: 25, 3: 25, 4: 25 }))
    renderCard()
    expect(screen.getByText(/Nothing to do/)).toBeTruthy()
  })

  it('counts the positions outside the band', () => {
    localStorage.setItem('allocationTargets', JSON.stringify({ 1: 50, 2: 10, 3: 25, 4: 15 }))
    renderCard()
    expect(screen.getByText(/3 position\(s\) outside the 5 pp band/)).toBeTruthy()
  })

  describe('adopt current weights', () => {
    it('writes today\'s weights as the targets', () => {
      localStorage.setItem('allocationTargets', JSON.stringify({ 1: 1 }))
      renderCard()
      fireEvent.click(screen.getByText('Adopt current weights'))
      expect(readTargets()).toEqual({ 1: 25, 2: 25, 3: 25, 4: 25 })
      expect(screen.getByText(/Nothing to do/)).toBeTruthy()
    })

    it('skips a position it cannot weigh', () => {
      localStorage.setItem('allocationTargets', JSON.stringify({ 1: 1 }))
      renderCard([pos(1, 'AAA', 500), pos(2, 'GHOST', 0, null)])
      fireEvent.click(screen.getByText('Adopt current weights'))
      expect(readTargets()).toEqual({ 1: 100 })
    })
  })

  it('clears every target on request', () => {
    localStorage.setItem('allocationTargets', JSON.stringify({ 1: 25, 2: 25 }))
    renderCard()
    fireEvent.click(screen.getByText('Clear targets'))
    expect(readTargets()).toEqual({})
  })

  it('gives a dual-listed symbol two independent inputs', () => {
    // Two securities share the ticker ASML, so a symbol-keyed target would
    // drive both from one field.
    localStorage.setItem('allocationTargets', JSON.stringify({ 7: 70, 8: 30 }))
    withProviders(<RebalanceCard positions={[pos(7, 'ASML', 600), pos(8, 'ASML', 400)]} />)
    const inputs = screen.getAllByLabelText('Target weight for ASML') as HTMLInputElement[]
    expect(inputs.map((i) => i.value).sort()).toEqual(['30', '70'])
  })

  describe('readTargets rejects what it cannot use', () => {
    it('survives corrupt JSON', () => {
      localStorage.setItem('allocationTargets', '{not json')
      expect(readTargets()).toEqual({})
    })

    it('drops non-numeric and out-of-range values rather than coercing them', () => {
      // A NaN here would propagate into every drift on the page.
      localStorage.setItem(
        'allocationTargets',
        JSON.stringify({ 1: 25, 2: 'twenty', 3: -5, 4: 140, 5: null }),
      )
      expect(readTargets()).toEqual({ 1: 25 })
    })
  })
})
