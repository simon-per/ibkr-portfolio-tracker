import { describe, expect, it } from 'vitest'
import { axisFloor, niceTicks, zeroBasedTicks } from './niceTicks'

describe('niceTicks', () => {
  it('produces round steps and brackets the data', () => {
    const { domain, ticks } = niceTicks(0, 47_300, 8)
    expect(domain[0]).toBeLessThanOrEqual(0)
    expect(domain[1]).toBeGreaterThanOrEqual(47_300)
    expect(ticks[0]).toBe(domain[0])
    expect(ticks[ticks.length - 1]).toBe(domain[1])
    for (const t of ticks) expect(Number.isInteger(t / 5000) || Number.isInteger(t)).toBe(true)
  })

  it('honours the target count, which is the whole point of the extraction', () => {
    // The old fixed 200/1000/2500/10000 ladder ignored this, which is why a 20k range
    // put eight labels into a 280px-tall phone chart.
    const wide = niceTicks(0, 20_000, 8)
    const narrow = niceTicks(0, 20_000, 4)
    expect(narrow.ticks.length).toBeLessThan(wide.ticks.length)
    expect(narrow.ticks.length).toBeGreaterThanOrEqual(3)
  })

  it('stays near the target across the ranges this portfolio actually spans', () => {
    // The band is what matters, not an exact hit: a nice step cannot land on every
    // count. What must not happen is a phone axis quietly carrying twice its target.
    for (const max of [800, 4_800, 12_500, 47_300, 260_000]) {
      for (const target of [4, 8]) {
        const n = niceTicks(0, max, target).ticks.length
        expect(n, `${max} @ ${target}`).toBeGreaterThanOrEqual(target - 2)
        expect(n, `${max} @ ${target}`).toBeLessThanOrEqual(target + 3)
      }
    }
  })

  it('handles a negative minimum — the profit/loss series goes below zero', () => {
    // The ForecastTab original clamped at 0 and never saw this case.
    const { domain, ticks } = niceTicks(-4_200, 11_800, 6)
    expect(domain[0]).toBeLessThanOrEqual(-4_200)
    expect(domain[1]).toBeGreaterThanOrEqual(11_800)
    expect(ticks.some(t => t < 0)).toBe(true)
    expect(ticks).toContain(0)
  })

  it('gives a flat series an axis with height instead of one repeated line', () => {
    const { domain, ticks } = niceTicks(500, 500, 5)
    expect(domain[0]).toBeLessThan(500)
    expect(domain[1]).toBeGreaterThan(500)
    expect(new Set(ticks).size).toBe(ticks.length)
  })

  it('does not accumulate floating-point drift across the tick run', () => {
    // `i += step` on a fractional step is how an axis label ends up reading
    // 2999.9999999999995.
    const { ticks } = niceTicks(0, 3, 10)
    for (const t of ticks) expect(Number(t.toFixed(10))).toBe(t)
  })

  it('reports the step its ticks are spaced by, which the axis label needs', () => {
    for (const [min, max, target] of [[0, 47_300, 8], [-4_200, 11_800, 6], [1_150, 1_310, 3], [500, 500, 5]]) {
      const { ticks, step } = niceTicks(min, max, target)
      for (let i = 1; i < ticks.length; i++) expect(ticks[i] - ticks[i - 1]).toBeCloseTo(step, 9)
    }
  })

  it('refuses rather than throws on degenerate input', () => {
    expect(niceTicks(NaN, 10, 5).ticks.length).toBeGreaterThan(0)
    expect(niceTicks(0, Infinity, 5).ticks.length).toBeGreaterThan(0)
    expect(niceTicks(0, 10, 0).ticks.length).toBeGreaterThan(0)
  })
})

// The production numbers these were written against, read off
// /api/portfolio/value-over-time on 2026-08-04: the three default series spanned
// +1,122 .. +65,025, so the padded minimum was -5,268 and nothing was negative at all.
const REAL_PADDED_MIN = -5_268
const REAL_PADDED_MAX = 71_517
const REAL_TRUE_MIN = 1_122

describe('niceTicks floor', () => {
  it('reproduces the bug when no floor is given, so the fix is not measuring itself', () => {
    // 4 ticks is the phone axis. Rounding -5,268 out to the 20k step gives -20,000:
    // a fifth of the chart reserved for values that do not exist in the data.
    const { domain } = niceTicks(REAL_PADDED_MIN, REAL_PADDED_MAX, 4)
    expect(domain[0]).toBe(-20_000)
  })

  it('puts the floor at zero when nothing in the data is negative', () => {
    const floor = axisFloor(REAL_TRUE_MIN, REAL_PADDED_MIN, -5_000)
    expect(floor).toBe(0)

    const { domain, ticks } = niceTicks(REAL_PADDED_MIN, REAL_PADDED_MAX, 4, floor)
    expect(domain[0]).toBe(0)
    expect(ticks.every(t => t >= 0)).toBe(true)
    // and the top is untouched, so the data still fits
    expect(domain[1]).toBeGreaterThanOrEqual(65_025)
  })

  it('never clips a real loss, even one deeper than the cap', () => {
    // The cap is soft on purpose: empty space is a cosmetic problem, a loss falling off
    // the bottom of the chart is a wrong number.
    const deep = -8_000
    const floor = axisFloor(deep, deep - 6_000, -5_000)
    expect(floor).toBeLessThanOrEqual(deep)

    const { domain } = niceTicks(deep - 6_000, 70_000, 4, floor)
    expect(domain[0]).toBeLessThanOrEqual(deep)
  })

  it('caps a shallow loss at the floor rather than a whole negative step', () => {
    const floor = axisFloor(-800, -7_200, -5_000)
    expect(floor).toBe(-5_000)

    const { domain, ticks } = niceTicks(-7_200, 71_517, 4, floor)
    expect(domain[0]).toBe(-5_000)
    expect(ticks).toContain(0)
    expect(ticks.every(t => t >= -5_000)).toBe(true)
  })

  it('ignores a floor that would leave nothing to draw', () => {
    // An inverted or empty domain renders as a blank chart in recharts, which reads as
    // an outage rather than a bad axis.
    const { domain, ticks } = niceTicks(0, 1_000, 5, 5_000)
    expect(domain[0]).toBeLessThan(domain[1])
    expect(ticks.length).toBeGreaterThan(0)
  })

  it('leaves every existing caller untouched when no floor is passed', () => {
    const without = niceTicks(-4_200, 11_800, 6)
    const explicitUndefined = niceTicks(-4_200, 11_800, 6, undefined)
    expect(explicitUndefined).toEqual(without)
    expect(without.ticks[0]).toBe(without.domain[0])
  })
})

describe('zeroBasedTicks', () => {
  it('runs through zero, which every bar is measured from', () => {
    expect(zeroBasedTicks([1_150, 1_300], 4).domain[0]).toBe(0)
    expect(zeroBasedTicks([-850, -120], 4).domain[1]).toBe(0)
    const { domain, ticks } = zeroBasedTicks([-2_400, 9_700], 4)
    expect(domain[0]).toBeLessThanOrEqual(-2_400)
    expect(domain[1]).toBeGreaterThanOrEqual(9_700)
    expect(ticks).toContain(0)
  })

  it('skips an unknown rather than letting NaN into the domain', () => {
    const { domain } = zeroBasedTicks([NaN, 400, NaN], 4)
    expect(domain).toEqual([0, 400])
    expect(zeroBasedTicks([], 4).ticks.every(Number.isFinite)).toBe(true)
  })
})

describe('axisFloor', () => {
  it('is never above the data minimum, whatever the cap', () => {
    for (const min of [-50_000, -8_000, -5_000, -4_999, -1, 0, 1_122, 65_025]) {
      const floor = axisFloor(min, min - 6_000, -5_000)
      if (min < 0) expect(floor, `min ${min}`).toBeLessThanOrEqual(min)
      else expect(floor, `min ${min}`).toBe(0)
    }
  })

  it('treats a non-finite minimum as zero rather than propagating NaN into the domain', () => {
    expect(axisFloor(NaN, -1_000, -5_000)).toBe(0)
  })
})
