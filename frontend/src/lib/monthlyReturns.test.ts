import { describe, expect, it } from 'vitest'
import { computeModifiedDietzReturn } from './monthlyReturns'
import type { PortfolioValuePoint } from './api'

function point(date: string, mv: number, cost: number, flow?: number): PortfolioValuePoint {
  return {
    date,
    cost_basis_eur: cost,
    market_value_eur: mv,
    gain_loss_eur: mv - cost,
    gain_loss_percent: cost > 0 ? ((mv - cost) / cost) * 100 : 0,
    external_flow_eur: flow,
  }
}

describe('computeModifiedDietzReturn', () => {
  it('returns plain price appreciation for a flowless month', () => {
    const r = computeModifiedDietzReturn([
      point('2026-03-01', 1000, 900, 0),
      point('2026-03-31', 1050, 900, 0),
    ])
    expect(r?.returnPercent).toBeCloseTo(5, 6)
  })

  it('a sale is not a loss', () => {
    // The regression: a lot bought at 110 sold for 200. Market value drops by
    // the proceeds (200), cost basis only by the cost (110). Inferring the flow
    // from the cost-basis line printed the 90 of realized gain as a −9% month.
    const r = computeModifiedDietzReturn([
      point('2026-03-01', 1000, 610, 0),
      point('2026-03-02', 800, 500, -200),
    ])
    expect(r?.returnPercent).toBeCloseTo(0, 6)
    expect(r?.newInvestment).toBe(-200)
  })

  it('a same-day rotation with a realized gain is flat, not negative', () => {
    // Sell A (cost 110) for 200, rebuy B at 200: the backend nets the day's
    // flow to zero. The cost-basis delta is +90, which used to read as a loss.
    const r = computeModifiedDietzReturn([
      point('2026-03-01', 1000, 610, 0),
      point('2026-03-02', 1000, 700, 0),
    ])
    expect(r?.returnPercent).toBeCloseTo(0, 6)
  })

  it('falls back to the cost-basis line when external_flow_eur is absent', () => {
    // Older payloads: right for purchases, which is all the fallback promises.
    const r = computeModifiedDietzReturn([
      point('2026-03-01', 1000, 1000),
      point('2026-03-02', 2010, 2000),
    ])
    expect(r?.returnPercent).toBeCloseTo(1, 6)
  })

  it('day-weights a mid-month flow', () => {
    // Flow of 300 lands after 1 of 2 days, so it earns half the period:
    // gain 130 over a denominator of 1000 + 300·0.5.
    const r = computeModifiedDietzReturn([
      point('2026-03-01', 1000, 1000, 0),
      point('2026-03-02', 1300, 1300, 300),
      point('2026-03-03', 1430, 1300, 0),
    ])
    expect(r?.returnPercent).toBeCloseTo((130 / 1150) * 100, 4)
  })

  it('refuses windows it cannot price', () => {
    expect(computeModifiedDietzReturn([point('2026-03-01', 1000, 1000, 0)])).toBeNull()
    expect(
      computeModifiedDietzReturn([
        point('2026-03-01', 0, 0, 0),
        point('2026-03-02', 500, 500, 500),
      ]),
    ).toBeNull()
  })
})

/**
 * The third consumer of `unpriced_holdings`, and the one missed when
 * `dailyReturnSeries` and `betaAndCorrelation` were guarded.
 *
 * Modified Dietz reads only the two endpoint values and the flows between them,
 * so an incomplete endpoint is not a small error. `market_value_eur` omits the
 * holdings the backend could not price while the flows do not, so a stale end
 * reads as a loss and a stale start as a gain — and past the backend's 14-day
 * lookback every holding drops out and the period prints -100%.
 */
describe('days the backend could not fully value', () => {
  function partialPoint(date: string, mv: number, cost: number, unpriced: number): PortfolioValuePoint {
    return { ...point(date, mv, cost, 0), unpriced_holdings: unpriced }
  }

  it('does not read a stalled feed at month end as a loss', () => {
    // Flat month, then the last day loses half the book to an unpriced holding.
    const r = computeModifiedDietzReturn([
      point('2026-03-01', 1000, 900, 0),
      point('2026-03-30', 1000, 900, 0),
      partialPoint('2026-03-31', 500, 900, 3),
    ])
    expect(r?.returnPercent).toBeCloseTo(0, 6)
    expect(r?.endValue).toBe(1000)
    expect(r?.partial).toBe(true)
    // The days it does cover, so a caller can say how much was dropped rather than only
    // that something was: the trimmed figure keeps the label of the whole period.
    expect(r?.measured).toEqual({ from: '2026-03-01', to: '2026-03-30' })
  })

  it('does not read a stalled feed at period start as a gain', () => {
    const r = computeModifiedDietzReturn([
      partialPoint('2026-03-01', 500, 900, 3),
      point('2026-03-02', 1000, 900, 0),
      point('2026-03-31', 1000, 900, 0),
    ])
    expect(r?.returnPercent).toBeCloseTo(0, 6)
    expect(r?.startValue).toBe(1000)
    expect(r?.partial).toBe(true)
    expect(r?.measured).toEqual({ from: '2026-03-02', to: '2026-03-31' })
  })

  it('sets the measured window exactly when it sets partial', () => {
    // They are written in one spread so they cannot separate — a `partial` with no window
    // would put the reader back to a bare dagger.
    const complete = computeModifiedDietzReturn([
      point('2026-03-01', 1000, 900, 0),
      point('2026-03-31', 1050, 900, 0),
    ])
    expect(complete?.partial).toBeUndefined()
    expect(complete?.measured).toBeUndefined()
  })

  it('never manufactures the -100% a fully unpriced tail produces', () => {
    // Every holding past the lookback: the backend reports 0.00 market value.
    const r = computeModifiedDietzReturn([
      point('2026-03-01', 1000, 900, 0),
      point('2026-03-30', 1020, 900, 0),
      partialPoint('2026-03-31', 0, 900, 5),
    ])
    expect(r?.returnPercent).toBeCloseTo(2, 6)
    expect(r?.returnPercent).toBeGreaterThan(-100)
  })

  it('leaves a complete period untouched and unflagged', () => {
    const r = computeModifiedDietzReturn([
      point('2026-03-01', 1000, 900, 0),
      point('2026-03-31', 1050, 900, 0),
    ])
    expect(r?.returnPercent).toBeCloseTo(5, 6)
    expect(r?.partial).toBeUndefined()
  })

  it('ignores an interior gap, which cannot affect the arithmetic', () => {
    // Nothing reads an interior market value, so trimming there would discard
    // real days for no gain — and would wrongly flag the period as partial.
    const r = computeModifiedDietzReturn([
      point('2026-03-01', 1000, 900, 0),
      partialPoint('2026-03-15', 400, 900, 2),
      point('2026-03-31', 1050, 900, 0),
    ])
    expect(r?.returnPercent).toBeCloseTo(5, 6)
    expect(r?.partial).toBeUndefined()
  })

  it('refuses rather than guessing when nothing in the period is measurable', () => {
    expect(computeModifiedDietzReturn([
      partialPoint('2026-03-01', 500, 900, 3),
      partialPoint('2026-03-31', 400, 900, 3),
    ])).toBeNull()
  })

  it('treats an absent field as complete, not as unmeasurable', () => {
    // The field only exists from 2026-08-05; reading `undefined` as incomplete
    // would flag every period served by an older backend.
    const r = computeModifiedDietzReturn([
      point('2026-03-01', 1000, 900, 0),
      point('2026-03-31', 1050, 900, 0),
    ])
    expect(r).not.toBeNull()
    expect(r?.partial).toBeUndefined()
  })

  it('refuses when there is nothing to divide by, rather than printing a flat month', () => {
    // Modified Dietz's denominator is `startMV + weightedFlow`, so an early outflow
    // larger than the opening value drives it to zero or below and the return is
    // undefined. It used to fall through to `0`, which renders as a real, quiet 0.00%
    // month — the one branch here that did not already refuse, while `startMV === 0` and
    // a single-point window both returned null. Defensive rather than observed: it needs
    // outflows exceeding the opening valuation, which a trimmed window can produce.
    expect(computeModifiedDietzReturn([
      point('2026-03-01', 1000, 1000, 0),
      point('2026-03-08', 0, 0, -2000),
      point('2026-03-15', 0, 0, 0),
      point('2026-03-22', 0, 0, 0),
      point('2026-03-31', 0, 0, 0),
    ])).toBeNull()
  })
})
