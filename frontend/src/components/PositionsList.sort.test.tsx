// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { PositionsList } from './PositionsList'
import type { Position } from '@/lib/api'

/**
 * The positions sort reads what the cells show (issue #24). It used to sort an unvalued
 * holding by the backend's stand-ins (value 0.00, gain -cost, -100%) while its cells print
 * dashes, an absent yield on cost as -1, and names by code unit. It goes through
 * `compareNullsLast` now, like every other sortable table.
 */

afterEach(cleanup)
/** A loaded summary from a backend that tracks no cash: weights are shares of holdings. */
const UNTRACKED_CASH = { amount: 0 }

function position(over: Partial<Position> & { security_id: number; symbol: string }): Position {
  return {
    description: `${over.symbol} Holdings`, isin: `ISIN${over.security_id}`, currency: 'EUR',
    exchange: 'NASDAQ', quantity: 10, cost_basis_eur: 100, market_value_eur: 200,
    market_price: 20, gain_loss_eur: 100, gain_loss_percent: 100, taxlots: [],
    analyst_rating: null, ...over,
  } as Position
}

// Priced: one winner, one loser.
const WIN = position({ security_id: 1, symbol: 'WIN', market_value_eur: 150, gain_loss_eur: 50, gain_loss_percent: 50 })
const LOSS = position({ security_id: 2, symbol: 'LOS', market_value_eur: 80, gain_loss_eur: -20, gain_loss_percent: -20 })
// What get_positions_breakdown sends for a holding it cannot price: value 0.00, gain
// -cost, -100%. The row renders dashes.
const UNPRICED = position({
  security_id: 3, symbol: 'UNP', cost_basis_eur: 500, market_value_eur: 0, market_price: null,
  gain_loss_eur: -500, gain_loss_percent: -100,
})

function order(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('tbody tr')).map((row) => {
    const t = row.textContent ?? ''
    return ['WIN', 'LOS', 'UNP', 'EGL', 'ZEB'].find((s) => t.includes(s)) ?? '?'
  })
}

/** The order after each of two clicks on a header: the first direction, then the flip. */
function bothWays(container: HTMLElement, header: string) {
  const button = screen.getByRole('button', { name: header })
  fireEvent.click(button)
  const first = order(container)
  fireEvent.click(button)
  return { first, second: order(container) }
}

describe('an unvalued holding in the positions sort', () => {
  it('sorts last on Gain, in both directions', () => {
    // Return is desktop-hidden; Gain carries the same stand-in (-cost).
    const { container } = render(<PositionsList positions={[WIN, UNPRICED, LOSS]} cash={UNTRACKED_CASH} />)
    expect(bothWays(container, 'Gain')).toEqual({
      first: ['WIN', 'LOS', 'UNP'], second: ['LOS', 'WIN', 'UNP'],
    })
  })

  it('sorts last on Value ascending, not first as a 0.00 holding', () => {
    const { container } = render(<PositionsList positions={[WIN, UNPRICED, LOSS]} cash={UNTRACKED_CASH} />)
    expect(order(container)).toEqual(['WIN', 'LOS', 'UNP'])
    fireEvent.click(screen.getByRole('button', { name: 'Value' })) // already sorted: flips to asc
    expect(order(container)).toEqual(['LOS', 'WIN', 'UNP'])
  })

  it('sorts last on Weight, in both directions', () => {
    const { container } = render(<PositionsList positions={[WIN, UNPRICED, LOSS]} cash={UNTRACKED_CASH} />)
    expect(bothWays(container, 'Weight')).toEqual({
      first: ['WIN', 'LOS', 'UNP'], second: ['LOS', 'WIN', 'UNP'],
    })
  })
})

describe('yield on cost in the positions sort', () => {
  it('sorts a holding with no rate last, in both directions', () => {
    // UNP is priced here: only its yield is absent.
    const payer = { ...UNPRICED, market_value_eur: 300, market_price: 30 }
    const yields = new Map<number, number | null>([[WIN.security_id, 4], [LOSS.security_id, 1]])
    const { container } = render(<PositionsList positions={[payer, WIN, LOSS]} yieldOnCost={yields} cash={UNTRACKED_CASH} />)
    expect(bothWays(container, 'YoC')).toEqual({
      first: ['WIN', 'LOS', 'UNP'], second: ['LOS', 'WIN', 'UNP'],
    })
  })
})

describe('the name sort', () => {
  const apple = position({ security_id: 6, symbol: 'WIN', description: 'Apple Inc' })
  const e = position({ security_id: 4, symbol: 'EGL', description: 'Électricité de France' })
  const z = position({ security_id: 5, symbol: 'ZEB', description: 'Zebra Technologies' })

  it('compares as a reader would (localeCompare), like every other table', () => {
    const { container } = render(<PositionsList positions={[z, e, apple]} cash={UNTRACKED_CASH} />)
    const { first, second } = bothWays(container, 'Position')
    // In either direction Électricité sits between Apple and Zebra.
    expect({ first: first[1], second: second[1] }).toEqual({ first: 'EGL', second: 'EGL' })
  })

  it('starts A to Z on the first click', () => {
    const { container } = render(<PositionsList positions={[z, e, apple]} cash={UNTRACKED_CASH} />)
    expect(bothWays(container, 'Position')).toEqual({
      first: ['WIN', 'EGL', 'ZEB'], second: ['ZEB', 'EGL', 'WIN'],
    })
  })
})
