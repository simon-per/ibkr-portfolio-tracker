// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render } from '@testing-library/react'
import { watchlistColumns } from './watchlistColumns'

/**
 * Review item U7: a phone card with no score and no analyst rating drew "-—" in its badge
 * row — the score cell's own "-" beside the analyst cell's dash. Both are view-aware now:
 * a dash in the table, nothing on the card.
 */
afterEach(cleanup)

describe('the watchlist badge cells', () => {
  const columns = watchlistColumns({ formatCurrency: () => '', currencySymbol: '$' })
  const bare = { buy_score: null, analyst_rating: null, target_upside: null } as never

  it('draw nothing on the card when there is nothing to show', () => {
    for (const key of ['buy_score', 'analyst_rating']) {
      const col = columns.find((c) => c.key === key)!
      expect(col.cell(bare, 'cards')).toBeNull()
    }
  })

  it('show only the upside on a card without a rating', () => {
    const col = columns.find((c) => c.key === 'analyst_rating')!
    const text = render(<>{col.cell({ ...(bare as object), target_upside: 0.5 } as never, 'cards')}</>)
      .container.textContent
    expect(text).toBe('+50% upside')
  })

  it('keep the dash in the table', () => {
    const col = columns.find((c) => c.key === 'buy_score')!
    expect(render(<>{col.cell(bare, 'table')}</>).container.textContent).toBe('—')
  })
})
