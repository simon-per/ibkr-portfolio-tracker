import { describe, expect, it } from 'vitest'
import { SPARK_H, SPARK_W, sparklineSegments } from './sparkline'

describe('sparklineSegments', () => {
  it('spans the box: first point at the left edge, last at the right, high at the top', () => {
    const drawn = sparklineSegments([1, 3, 2])!
    const [pts] = drawn.segments
    expect(pts[0][0]).toBe(0)
    expect(pts[2][0]).toBe(SPARK_W)
    // 3 is the max, so it sits highest (smallest y); 1 the lowest.
    expect(pts[1][1]).toBeLessThan(pts[2][1])
    expect(pts[0][1]).toBeGreaterThan(pts[2][1])
    expect(drawn.first).toBe(1)
    expect(drawn.last).toBe(2)
  })

  it('breaks the line at an unknown rather than drawing it as a zero', () => {
    const drawn = sparklineSegments([5, 6, null, 7, Number.NaN, 8, 9])!
    expect(drawn.segments.map((s) => s.length)).toEqual([2, 1, 2])
    // A gap keeps its place on the time axis.
    expect(drawn.segments[1][0][0]).toBeCloseTo((3 / 6) * SPARK_W)
    for (const seg of drawn.segments) for (const [, y] of seg) expect(Number.isFinite(y)).toBe(true)
  })

  it('draws a flat series along the middle instead of dividing by zero', () => {
    const drawn = sparklineSegments([4, 4, 4])!
    for (const [, y] of drawn.segments[0]) expect(y).toBe(SPARK_H / 2)
  })

  it('returns nothing to draw below two measurable points', () => {
    expect(sparklineSegments([])).toBeNull()
    expect(sparklineSegments([3])).toBeNull()
    expect(sparklineSegments([null, 3, undefined])).toBeNull()
  })
})
