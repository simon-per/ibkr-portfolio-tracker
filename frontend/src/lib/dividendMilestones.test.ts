import { describe, expect, it } from 'vitest'
import { forecastMilestones, milestoneStep } from './dividendMilestones'

describe('milestoneStep', () => {
  it('picks 1, 2 or 5 times a power of ten, at most a third of the figure', () => {
    expect(milestoneStep(375)).toBe(100)
    expect(milestoneStep(1500)).toBe(500)
    expect(milestoneStep(60)).toBe(20)
    expect(milestoneStep(3000)).toBe(1000)
    expect(milestoneStep(250)).toBe(50)
    expect(milestoneStep(4.2)).toBe(1)
  })

  it('has no step for a figure that is not positive', () => {
    expect(milestoneStep(0)).toBeNull()
    expect(milestoneStep(-5)).toBeNull()
    expect(milestoneStep(Number.NaN)).toBeNull()
  })
})

describe('forecastMilestones', () => {
  const line = (...pairs: [string, number][]) => pairs.map(([date, value]) => ({ date, value }))

  it('marks the first day at or above each level since the start, and the next level', () => {
    const m = forecastMilestones(line(
      ['2026-08-28', 180], ['2026-09-10', 205], ['2026-09-20', 198],
      ['2026-10-02', 301.5], ['2026-11-05', 364.84],
    ))!
    expect(m.step).toBe(100)
    // 200 on the first day above it, not again after the dip; 300 on the jump.
    expect(m.crossed).toEqual([
      { level: 200, date: '2026-09-10', value: 205 },
      { level: 300, date: '2026-10-02', value: 301.5 },
    ])
    expect(m.next).toBe(400)
    expect(m.toGo).toBeCloseTo(35.16)
  })

  it('crosses two levels on one day when the line jumps them', () => {
    const m = forecastMilestones(line(['2026-08-28', 90], ['2026-09-01', 320]))!
    expect(m.crossed.map((c) => c.level)).toEqual([100, 200, 300])
    expect(new Set(m.crossed.map((c) => c.date))).toEqual(new Set(['2026-09-01']))
  })

  it('counts nothing the start day already stood above, and nothing for a falling line', () => {
    const m = forecastMilestones(line(['2026-08-28', 310], ['2026-09-01', 290]))!
    expect(m.crossed).toEqual([])
    expect(m.next).toBe(300)
    expect(m.toGo).toBeCloseTo(10)
  })

  it('needs two days and a positive figure', () => {
    expect(forecastMilestones(line(['2026-08-28', 300]))).toBeNull()
    expect(forecastMilestones(line(['2026-08-28', 300], ['2026-09-01', 0]))).toBeNull()
  })
})
