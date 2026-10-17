import { describe, expect, it } from 'vitest'
import { earningsBeats } from './earningsBeats'
import type { EarningsHistoryItem } from './api'

const item = (beat_or_miss: string | null): EarningsHistoryItem => ({
  security_id: 1, symbol: 'AAA', description: '', earnings_date: '2026-07-24',
  eps_estimate: null, reported_eps: null, surprise_percent: null, beat_or_miss,
})

describe('earningsBeats', () => {
  it('counts beats out of the quarters that have a verdict', () => {
    expect(earningsBeats([item('Beat'), item('Miss'), item('Met'), item('Beat'), item(null)]))
      .toEqual({ beats: 2, judged: 4 })
  })

  it('is null, not "0 of 0", when nothing has a verdict', () => {
    expect(earningsBeats([])).toBeNull()
    expect(earningsBeats([item(null)])).toBeNull()
  })
})
