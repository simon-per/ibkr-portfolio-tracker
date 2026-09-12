import { describe, expect, it } from 'vitest'
import { UNRATED_SCORE, getRatingScore } from './analystRating'

/**
 * The scale itself. `components/ratingSortFamily.test.tsx` is the family half: it sorts
 * one fixture through both tables that use this and requires the same order.
 */

describe('getRatingScore', () => {
  it('scores the backend spelling and the display spelling identically', () => {
    const pairs = [
      ['strong_buy', 'Strong Buy'],
      ['buy', 'Buy'],
      ['hold', 'Hold'],
      ['sell', 'Sell'],
      ['strong_sell', 'Strong Sell'],
    ]
    for (const [snake, display] of pairs) {
      expect(getRatingScore(snake)).toBe(getRatingScore(display))
    }
    // And the spellings in between, since a rating is an enum and not a string.
    expect(getRatingScore('strong-buy')).toBe(getRatingScore('strong_buy'))
    expect(getRatingScore(' STRONG BUY ')).toBe(getRatingScore('strong_buy'))
  })

  it('orders by conviction, strongest highest, with no ties', () => {
    const scores = ['strong_buy', 'buy', 'hold', 'sell', 'strong_sell'].map(getRatingScore)
    expect(scores).toEqual([...scores].sort((a, b) => b - a))
    expect(new Set(scores).size).toBe(5)
  })

  it('puts the unrated below every real rating', () => {
    for (const value of [null, undefined, '', 'n/a', 'no coverage']) {
      expect(getRatingScore(value)).toBe(UNRATED_SCORE)
    }
    expect(getRatingScore('strong_sell')).toBeGreaterThan(UNRATED_SCORE)
  })

  it('descending on the score leads with strong_buy, where the old comparator led with strong_sell', () => {
    const ratings = ['hold', 'strong_sell', 'strong_buy', null, 'sell', 'buy']
    const byScore = [...ratings].sort((a, b) => getRatingScore(b) - getRatingScore(a))
    expect(byScore).toEqual(['strong_buy', 'buy', 'hold', 'sell', 'strong_sell', null])
    // The watchlist's `localeCompare` descending: "se" sorts after "bu".
    const bySpelling = ['strong_buy', 'strong_sell'].sort((a, b) => b.localeCompare(a))
    expect(bySpelling[0]).toBe('strong_sell')
  })
})
