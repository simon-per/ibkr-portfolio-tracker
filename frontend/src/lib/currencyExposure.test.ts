import { describe, expect, it } from 'vitest'
import {
  UNKNOWN_CURRENCY,
  computeCurrencyExposure,
  foreignQuotedPct,
  type CurrencyExposureInput,
} from './currencyExposure'

function pos(
  security_id: number,
  symbol: string,
  currency: string | null,
  market_value_eur: number,
  market_price: number | null = 10,
): CurrencyExposureInput {
  return { security_id, symbol, currency, market_value_eur, market_price }
}

describe('computeCurrencyExposure', () => {
  it('groups by quote currency and weights by value', () => {
    const exposure = computeCurrencyExposure([
      pos(1, 'AAPL', 'USD', 600),
      pos(2, 'MSFT', 'USD', 200),
      pos(3, 'SAP', 'EUR', 200),
    ])
    expect(exposure.totalValue).toBe(1000)
    expect(exposure.rows.map((r) => [r.currency, r.pct])).toEqual([
      ['USD', 80],
      ['EUR', 20],
    ])
    expect(exposure.rows[0].positionCount).toBe(2)
  })

  it('sorts by value, largest first', () => {
    const exposure = computeCurrencyExposure([
      pos(1, 'A', 'CAD', 100),
      pos(2, 'B', 'USD', 500),
      pos(3, 'C', 'EUR', 300),
    ])
    expect(exposure.rows.map((r) => r.currency)).toEqual(['USD', 'EUR', 'CAD'])
  })

  it('normalises casing and whitespace so one currency is one bucket', () => {
    const exposure = computeCurrencyExposure([
      pos(1, 'A', 'usd', 500),
      pos(2, 'B', ' USD ', 500),
    ])
    expect(exposure.rows).toHaveLength(1)
    expect(exposure.rows[0].currency).toBe('USD')
  })

  describe('an unpriced position carries no currency weight', () => {
    it('is excluded from the total and counted', () => {
      // The portfolio values a no-price holding at 0.00, so folding it in would
      // understate its currency by exactly that position.
      const exposure = computeCurrencyExposure([
        pos(1, 'AAPL', 'USD', 500),
        pos(2, 'GHOST', 'CAD', 0, null),
      ])
      expect(exposure.totalValue).toBe(500)
      expect(exposure.unpricedCount).toBe(1)
      expect(exposure.rows.map((r) => r.currency)).toEqual(['USD'])
    })

    it('does not create a 0% bucket for it', () => {
      const exposure = computeCurrencyExposure([
        pos(1, 'AAPL', 'USD', 500),
        pos(2, 'GHOST', 'CAD', 0, null),
      ])
      expect(exposure.rows.find((r) => r.currency === 'CAD')).toBeUndefined()
    })

    it('excludes a priced holding worth zero, which means its FX rate is missing', () => {
      // Was asserted the other way, on the false premise that this is a fully-sold
      // holding — those have no open lots and never reach here. A zero value with a price
      // set means the FX conversion failed, so the holding contributes nothing to its
      // bucket and belongs in the count where that caveat is stated.
      const exposure = computeCurrencyExposure([
        pos(1, 'AAPL', 'USD', 500),
        pos(2, 'NOFX', 'CAD', 0, 42),
      ])
      expect(exposure.rows.find((r) => r.currency === 'CAD')).toBeUndefined()
      expect(exposure.unpricedCount).toBe(1)
    })
  })

  describe('a missing currency is its own bucket', () => {
    it('does not drop the position', () => {
      const exposure = computeCurrencyExposure([pos(1, 'A', null, 400), pos(2, 'B', 'EUR', 600)])
      expect(exposure.totalValue).toBe(1000)
      expect(exposure.rows.find((r) => r.currency === UNKNOWN_CURRENCY)?.pct).toBe(40)
    })

    it('treats blank the same as absent, and not as the base currency', () => {
      const exposure = computeCurrencyExposure([pos(1, 'A', '   ', 500), pos(2, 'B', 'EUR', 500)])
      expect(exposure.rows.find((r) => r.currency === UNKNOWN_CURRENCY)?.positionCount).toBe(1)
      expect(exposure.rows.find((r) => r.currency === 'EUR')?.positionCount).toBe(1)
    })
  })

  describe('funds are named, never re-attributed', () => {
    // SXR8 is a EUR-listed S&P 500 tracker: quote currency EUR, exposure USD.
    const book = [pos(1, 'SXR8', 'EUR', 400), pos(2, 'SAP', 'EUR', 100), pos(3, 'AAPL', 'USD', 500)]
    const funds = new Set(['SXR8'])

    it('leaves the fund in its quote-currency bucket', () => {
      // Moving it to USD would be inventing a split the data cannot support.
      const exposure = computeCurrencyExposure(book, funds)
      expect(exposure.rows.find((r) => r.currency === 'EUR')?.marketValue).toBe(500)
    })

    it('reports how much of the book the caveat applies to', () => {
      const exposure = computeCurrencyExposure(book, funds)
      expect(exposure.lookThroughValue).toBe(400)
      expect(exposure.lookThroughPct).toBe(40)
      expect(exposure.lookThroughCount).toBe(1)
    })

    it('attributes the fund share within its own currency row', () => {
      const exposure = computeCurrencyExposure(book, funds)
      const eur = exposure.rows.find((r) => r.currency === 'EUR')!
      expect(eur.fundCount).toBe(1)
      expect(eur.fundValue).toBe(400)
      // The direct holding in the same currency is not implicated.
      expect(eur.positionCount).toBe(2)
    })

    it('reports no caveat when nothing held is a fund', () => {
      const exposure = computeCurrencyExposure(book)
      expect(exposure.lookThroughCount).toBe(0)
      expect(exposure.lookThroughPct).toBe(0)
      expect(exposure.rows.every((r) => r.fundCount === 0)).toBe(true)
    })

    it('ignores an unpriced fund, as for any unpriced position', () => {
      const exposure = computeCurrencyExposure(
        [pos(1, 'SXR8', 'EUR', 0, null), pos(2, 'AAPL', 'USD', 500)],
        funds,
      )
      expect(exposure.lookThroughCount).toBe(0)
      expect(exposure.unpricedCount).toBe(1)
    })
  })

  describe('degenerate inputs', () => {
    it('handles an empty portfolio without dividing by zero', () => {
      const exposure = computeCurrencyExposure([])
      expect(exposure.rows).toEqual([])
      expect(exposure.totalValue).toBe(0)
      // A share of nothing is absent, not 0% — the server's `share_pct` rule.
      expect(exposure.lookThroughPct).toBeNull()
    })

    it('reports no rows at all when nothing can be valued', () => {
      // A zero-valued priced holding is now unpriced (its FX rate is missing), so there
      // is no row to give a percent to — which is the honest answer, and matches
      // foreignQuotedPct returning null rather than 0 on the same input.
      const exposure = computeCurrencyExposure([pos(1, 'A', 'EUR', 0, 5)], new Set(['A']))
      expect(exposure.rows).toHaveLength(0)
      expect(exposure.unpricedCount).toBe(1)
      expect(exposure.lookThroughPct).toBeNull()
    })
  })
})

describe('foreignQuotedPct', () => {
  const book = [pos(1, 'AAPL', 'USD', 700), pos(2, 'SAP', 'EUR', 200), pos(3, 'NESN', 'CHF', 100)]

  it('is everything not quoted in the base currency', () => {
    expect(foreignQuotedPct(computeCurrencyExposure(book), 'CHF')).toBeCloseTo(90, 10)
    expect(foreignQuotedPct(computeCurrencyExposure(book), 'USD')).toBeCloseTo(30, 10)
  })

  it('is 100% when nothing is quoted in the base currency', () => {
    expect(foreignQuotedPct(computeCurrencyExposure(book), 'GBP')).toBeCloseTo(100, 10)
  })

  it('matches the base currency case-insensitively', () => {
    expect(foreignQuotedPct(computeCurrencyExposure(book), 'chf')).toBeCloseTo(90, 10)
  })

  it('is null rather than zero when nothing is priced', () => {
    // No positions is an unknown exposure, not an unhedged-free one.
    expect(foreignQuotedPct(computeCurrencyExposure([]), 'CHF')).toBeNull()
  })
})
