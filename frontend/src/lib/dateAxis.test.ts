import { describe, expect, it } from 'vitest'
import { dateAxis } from './dateAxis'

function days(from: string, count: number): string[] {
  const [y, m, d] = from.split('-').map(Number)
  return Array.from({ length: count }, (_, i) => {
    const date = new Date(y, m - 1, d + i)
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
  })
}

describe('dateAxis', () => {
  it('names the day on a 3M span, so no two ticks can read the same', () => {
    const series = days('2026-07-15', 92)
    const axis = dateAxis(series)
    expect(axis.ticks).toBeUndefined()
    const labels = series.map(axis.tickFormatter)
    expect(new Set(labels).size).toBe(labels.length)
    expect(axis.tickFormatter('2026-08-03')).toBe('Aug 3')
  })

  it('puts one tick on each month of a longer span, labelled once', () => {
    const series = days('2025-10-16', 365)
    const axis = dateAxis(series)
    expect(axis.ticks?.[0]).toBe('2025-10-16')
    expect(axis.ticks?.[1]).toBe('2025-11-01')
    expect(axis.ticks).toHaveLength(13)
    const labels = axis.ticks!.map(axis.tickFormatter)
    expect(new Set(labels).size).toBe(labels.length)
    expect(labels[1]).toBe('Nov 25')
  })

  it('ticks on the first point a month has, not on the 1st', () => {
    const axis = dateAxis(['2026-01-02', '2026-01-05', '2026-02-02', '2026-03-02', '2026-04-01', '2026-05-04'])
    expect(axis.ticks).toEqual(['2026-01-02', '2026-02-02', '2026-03-02', '2026-04-01', '2026-05-04'])
  })

  it('copes with no data', () => {
    expect(dateAxis([]).tickFormatter('2026-01-01')).toBe('Jan 1')
  })
})
