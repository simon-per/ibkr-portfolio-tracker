import { describe, expect, it } from 'vitest'
import type { DividendTtmPoint } from './api'
import { dividendPace } from './dividendPace'

/** A rolling window ending in `month` and totalling `total`. */
function pt(month: string, total: number, forecast = 0): DividendTtmPoint {
  return {
    month, actual: {}, forecast: {},
    net_eur: total - forecast, forecast_net_eur: forecast, total_eur: total,
    mom_pct: null, mom_includes_forecast: forecast > 0,
    source: 'ibkr', mom_crosses_era: false, partial: forecast > 0,
  }
}

/** n+1 windows doubling over n months, so the rate has a closed form. */
function doubling(start: string, months: number, from = 100): DividendTtmPoint[] {
  const [y, m] = start.split('-').map(Number)
  return Array.from({ length: months + 1 }, (_, i) => {
    const t = y * 12 + (m - 1) + i
    const key = `${String(Math.floor(t / 12)).padStart(4, '0')}-${String(t % 12 + 1).padStart(2, '0')}`
    return pt(key, from * 2 ** (i / months))
  })
}

describe('the dividend growth pace', () => {
  it('answers the range it is given, not a fixed lookback', () => {
    // The whole reason this moved off the server. The previous version measured a
    // six-month window anchored at the projection horizon, so every range reported
    // the same number while the reader changed the filter above it.
    const series = doubling('2025-01', 24)
    const whole = dividendPace(series, null)!
    const tail = dividendPace(series.slice(-7), null)!

    expect(whole.months).toBe(24)
    expect(tail.months).toBe(6)
    expect(whole.from_month).toBe('2025-01')
    expect(tail.from_month).toBe('2026-07')
    // Same underlying curve, so the rates agree — but the SPAN and the anchors
    // must follow the slice, which is what the reader is looking at.
    expect(tail.cmgr_pct).toBeCloseTo(whole.cmgr_pct, 1)
    expect(tail.to_month).toBe(whole.to_month)
  })

  it('anchors on the first and last window shown, whatever lies between', () => {
    const pace = dividendPace([pt('2026-01', 100), pt('2026-02', 5), pt('2026-07', 200)], null)!
    expect([pace.from_month, pace.to_month]).toEqual(['2026-01', '2026-07'])
    expect([pace.from_eur, pace.to_eur]).toEqual([100, 200])
    expect(pace.months).toBe(6)
  })

  it('compounds back to the two windows it names', () => {
    // The figure sits beside those two totals, so the one thing it must not do is
    // disagree with them. An arithmetic mean of the monthly steps would.
    const pace = dividendPace(doubling('2025-01', 12), null)!
    // Two decimals on the published rate, so recompounding it over twelve steps
    // drifts by ~6e-4 — the rounding, not the arithmetic.
    expect((1 + pace.cmgr_pct / 100) ** pace.months)
      .toBeCloseTo(pace.to_eur / pace.from_eur, 2)
    // ...and the annual figure is the monthly one compounded, never times twelve.
    expect(1 + pace.cagr_pct! / 100).toBeCloseTo((1 + pace.cmgr_pct / 100) ** 12, 2)
    expect(pace.cagr_pct).toBeCloseTo(100, 1)   // doubled in twelve months
  })

  it('annualizes only once there is a year of span to annualize from', () => {
    // Pinned at the edge. Seven months of a funding ramp compounds to four
    // figures, which says more about the exponent than about the portfolio.
    expect(dividendPace(doubling('2025-01', 11), null)!.cagr_pct).toBeNull()
    expect(dividendPace(doubling('2025-01', 12), null)!.cagr_pct).not.toBeNull()
  })

  it('marks a base that is the earliest window on record', () => {
    const series = doubling('2025-05', 12)
    expect(dividendPace(series, '2025-05')!.coverage_limited).toBe(true)
    // The same series seen from a later range is not coverage-limited...
    expect(dividendPace(series.slice(3), '2025-05')!.coverage_limited).toBe(false)
    // ...and neither is one whose server did not say where coverage began.
    expect(dividendPace(series, null)!.coverage_limited).toBe(false)
  })

  it('reads forward-looking off the anchors, not off a flag', () => {
    expect(dividendPace([pt('2026-01', 100), pt('2026-07', 200)], null)!.includes_forecast)
      .toBe(false)
    expect(dividendPace([pt('2026-01', 100), pt('2026-07', 200, 50)], null)!.includes_forecast)
      .toBe(true)
    // A projected BASE counts too — the comparison is against a guess either way.
    expect(dividendPace([pt('2026-01', 100, 10), pt('2026-07', 200)], null)!.includes_forecast)
      .toBe(true)
  })

  it('falls to zero as a real rate but never grows out of nothing', () => {
    // A payer stopping takes the window to zero, and -100%/month is the honest
    // reading of that. Growing FROM zero is undefined, not large.
    expect(dividendPace([pt('2026-01', 60), pt('2026-07', 0)], null)!.cmgr_pct).toBe(-100)
    expect(dividendPace([pt('2026-01', 0), pt('2026-07', 60)], null)).toBeNull()
  })

  it('is absent rather than zero when there is nothing to measure', () => {
    // 0.0%/month reads as "flat", which is an answer none of these have earned.
    expect(dividendPace([], null)).toBeNull()
    expect(dividendPace([pt('2026-01', 100)], null)).toBeNull()
    expect(dividendPace([pt('2026-01', 100), pt('2026-01', 200)], null)).toBeNull()
  })
})
